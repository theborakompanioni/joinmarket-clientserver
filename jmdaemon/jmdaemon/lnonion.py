from jmdaemon.message_channel import MessageChannel
from jmdaemon.protocol import COMMAND_PREFIX
from jmbase.support import get_log, bintohex, hextobin
from pyln.client import LightningRpc
from io import BytesIO
import struct
import json
from twisted.internet import reactor, task
from twisted.internet.protocol import Protocol, ServerFactory
from twisted.protocols.basic import LineReceiver
log = get_log()

LOCAL_CONTROL_MESSAGE_TYPES = {"connect": 785, "disconnect": 787}
CONTROL_MESSAGE_TYPES = {"peerlist": 789, "getpeerlist": 791}
JM_MESSAGE_TYPES = {"privmsg": 685, "pubmsg": 687}

# Used for some control message construction, as detailed below.
NICK_PEERLOCATOR_SEPARATOR = ";"

"""
### MESSAGE FORMAT USED on the LN-ONION CHANNELS

( || means concatenation for strings, here)

`rawtlv` as defined in https://lightning.readthedocs.io/lightning-sendonionmessage.7.html
is hex-encoded and therefore is of form:

hex-encoded-varint-message-type || hex-encoded-length-of-message || hex-encoded-message

This encoding must happen here (though, as explained below, the receiving side is different).

The rest of this description is how the third field, `message`, is created, and read:

Since we cannot assume a 1:1 mapping of `nick` to lightning daemon (and therefore peerid),
we prepend with both the Joinmarket nicks (this is slightly awkward but keeps compatibility
with existing IRC message channels).

In text, the message is therefore:

from-nick || COMMAND_PREFIX || to-nick || COMMAND_PREFIX || cmd || " " || innermessage

(COMMAND_PREFIX defined in this package's protocol.py)

Here `innermessage` may be a list of messages (e.g. the multiple offer case) separated by COMMAND PREFIX.

Note that this syntax will still be as was described here:

https://github.com/JoinMarket-Org/JoinMarket-Docs/blob/master/Joinmarket-messaging-protocol.md#joinmarket-messaging-protocol

Note also that there is no chunking requirement applied here, based on the assumption that we have a sufficient 1300 byte limit.
That'll probably be changed later.

### CONTROL MESSAGES

The messages `getpeerlist` and `peerlist` are special messages sent to and
from directory servers.

The syntax of `getpeerlist` is:

from-nick || NICK_PEERLOCATOR_SEPARATOR || peer-location

`from_nick` must be a valid Joinmarket nick belonging to the sending node.
The `peer-location` field can either be a 66 character hex-encoded pubkey or a full peer location
as `pubkey@host:port`.

This message is crucial to the network function, since only from here does the directory node know
the match between the nick (which is cryptographically signed in Joinmarket across channels) and the
LN node pubkey. Notice that more than one nick is NOT allowed per LN node; this is deferred to
future updates.

The syntax of `peerlist` is:

nick || NICK_PEERLOCATOR_SEPARATOR || peer-location || "," ... (repeated)

i.e. a serialization of two-element tuples, each of which is a Joinmarket nick followed by a peer
location, as for the `getpeerlist` message.

#### LOCAL CONTROL MESSAGES

There are two messages passed inside the plugin, to Joinmarketd, in response to events in lightningd,
namely the `connect` and `disconnect` events triggered at the LN level. These are used to update
the *state* of existing peers that we have recorded as connected at some point, to ourselves.

The mechanisms here are loosely synchronizing a database of JM peers, with obviously the directory
node(s) acting as a queryable resource. It's notable that there are no guarantees of accuracy or
synchrony here.

PARSING OF RECEIVED JM_MESSAGES
===

The text will be utf-encoded before being hexlified, and therefore the following actions are needed of the receiver:

Extract rawtlv field which is received as encoded json:

json.loads(msg.decode("utf-8"))["unknown_fields"][0]["value"]

(the ["number"] key is used to check for control messages, see below for details).

Then: unhexlify, converting to binary, then .decode("utf-8") again, converting to a string.

Split the string by COMMAND PREFIX and parse as `from nick, to nick, command, message(s)`.

These arguments can be passed into Joinmarket's normal message channel processing, and should be
identical to that coming from IRC.

However, before doing so, we need to identify "public messages" versus private, which does not
have as natural a meaning here as it does on IRC; we impose it by using a to-nick value of PUBLIC
and by sending the message type `687` to the lightning plugin `jmcl.py` instead of the default
message type `685` for privmsgs to a single counterparty. This will instruct the directory server
to send the message to every peer it knows about.
"""

""" this passthrough protocol allows
    the joinmarket daemon to receive messages
    from some outside process, instead of from
    a client connection created in this process.
    We use a LineReceiver as the app-layer distinguisher
    of individual messages.
"""
class TCPPassThroughProtocol(LineReceiver):
    def __init__(self, factory):
        self.factory = factory

    def connectionMade(self):
        print("connection made in jmd")

    def connectionLost(self, reason):
        print("connection lost in jmd")

    def lineReceived(self, line):
        """ Data passed over this TCP socket
        is assumed to be JSON encoded, only.
        """
        try:
            data = line.decode("utf-8")
        except UnicodeDecodeError:
            log.warn("Received invalid data over the wire, ignoring.")
            return
        if len(self.factory.listeners) == 0:
            log.msg("WARNING! We received: {} but there "
                    "were no listeners.".format(data))
        for listener in self.factory.listeners:
            try:
                listener.receive_msg(json.loads(data))
            except json.decoder.JSONDecodeError as e:
                log.error("Error receiving data: {}, {}".format(data, repr(e)))


class TCPPassThroughFactory(ServerFactory):
    # the protocol created here is a singleton,
    # global to the joinmarket(d) process;
    # we interact with it from multiple Joinmarket
    # protocol instantiations by adding listeners
    # (because all it does is listen).
    # Listeners are added to the factory, not the
    # protocol instance as that won't exist until
    # someone connects.
    def __init__(self):
        # listeners will all receive
        # all messages; they must have
        # a `receive_msg` method that
        # process messages in JSON.
        self.listeners = []

    def buildProtocol(self, addr):
        self.p = TCPPassThroughProtocol(self)
        return self.p
    def add_tcp_listener(self, listener):
        self.listeners.append(listener)

    def remove_tcp_listener(self, listener):
        try:
            self.listeners.remove(listener)
        except ValueError:
            pass

""" Taken from @cdecker's https://github.com/lightningd/plugins/blob/master/noise/primitives.py
    Of note: we could import this functionality from our backend (jmbitcoin <- python-bitcointx),
    however jmdaemon must not rely on the jmbitcoin package.
"""
def varint_encode(i, w):
    """Encode an integer `i` into the writer `w`
    """
    if i < 0xFD:
        w.write(struct.pack("!B", i))
    elif i <= 0xFFFF:
        w.write(struct.pack("!BH", 0xFD, i))
    elif i <= 0xFFFFFFFF:
        w.write(struct.pack("!BL", 0xFE, i))
    else:
        w.write(struct.pack("!BQ", 0xFF, i))

def int_to_var_bytes(i):
    b = BytesIO()
    varint_encode(i, b)
    return b.getvalue()

class LNOnionPeerError(Exception):
    pass

class LNOnionPeerIDError(LNOnionPeerError):
    pass

class LNOnionPeerDirectoryWithoutHostError(LNOnionPeerError):
    pass

class LNOnionPeerConnectionError(LNOnionPeerError):
    pass

class LNOnionPeer(object):

    def __init__(self, peerid: str, hostname: str=None,
                 port: int=-1, directory: bool=False,
                 nick: str=""):
        if not len(peerid) == 66:
            # TODO: check valid pubkey without jmbitcoin?
            raise LNOnionPeerIDError()
        self.peerid = peerid
        self.nick = nick
        self.hostname = hostname
        self.port = port
        if directory and not (self.hostname):
            raise LNOnionPeerDirectoryWithoutHostError()
        self.directory = directory
        self.is_connected = False

    def set_nick(self, nick):
        self.nick = nick

    def get_nick_peerlocation_ser(self):
        if not self.nick:
            raise LNOnionPeerError("Cannot serialize "
                "identifier string without nick.")
        return self.nick + NICK_PEERLOCATOR_SEPARATOR + \
               self.peer_location_or_id()

    @classmethod
    def from_location_string(cls, locstr: str,
                directory: bool=False):
        peerid, hostport = locstr.split("@")
        host, port = hostport.split(":")
        port = int(port)
        return cls(peerid, host, port, directory)

    def set_host_port(self, hostname: str, port: int) -> None:
        """ If the connection info is discovered
        after this peer was already added to our list,
        we can set it with this method.
        """
        self.hostname = hostname
        self.port = port

    def peer_location_or_id(self):
        try:
            return self.peer_location()
        except AssertionError:
            return self.peerid

    def peer_location(self):
        assert (self.hostname and self.port > 0)
        return self.peerid + "@" + self.hostname + ":" + str(self.port)

    def connect(self, rpcclient: LightningRpc) -> None:
        """ This method is called to fire the RPC `connect`
        call to the LN peer associated with this instance.
        """
        if self.is_connected:
            return
        if not (self.hostname and self.port > 0):
            raise LNOnionPeerConnectionError(
                "Cannot connect without host, port info")
        rpcclient.call("connect", [self.peer_location()])
        self.is_connected = True

    def disconnect(self, rpcclient: LightningRpc) -> None:
        if not self.is_connected:
            return
        if not (self.hostname and self.port > 0):
            raise LNOnionPeerConnectionError(
                "Cannot disconnect without host, port info")
        rpcclient.call("disconnect", [self.peer_location()])
        self.is_connected = False

class LNOnionMessage(object):
    """ Encapsulates the messages passed over the wire
    from c-lightning, allow conversion into the text
    strings used in Joinmarket message channels.
    """
    def __init__(self, text: str, msgtype: int):
        self.text = text
        self.msgtype = msgtype

    def encode(self) -> str:
        bintext = self.text.encode("utf-8")
        hextext = bintohex(bintext)
        self.encoded = bintohex(int_to_var_bytes(
            self.msgtype)) + bintohex(int_to_var_bytes(
            len(bintext))) + hextext
        return self.encoded

    @classmethod
    def from_sendonionmessage_decode(cls, msg:
                    str, msgtype: int):
        """ This is not the reverse operation to encode,
        since we receive the messages as output of
        c-lightning's `sendonionmessage`.
        """
        text = hextobin(msg).decode("utf-8")
        return cls(text, msgtype)

class LNOnionMessageChannel(MessageChannel):
    """ Uses the plugin architecture to hook
    the `sendonionmessage` feature of c-lightning
    to receive messages over the LN onion network,
    and the provided RPC client LightningRPC to send
    messages.
    See the file jmcl.py for the actual Lightning plugin,
    which must be loaded in a running instance of c-lightning,
    for this to work.
    Uses one or more configured "directory nodes"
    to access a list of current active nodes, and updates
    dynamically from messages seen.
    """
    test_index = 1
    def __init__(self,
                 configdata,
                 daemon=None):
        MessageChannel.__init__(self, daemon=daemon)
        # configures access to c-lightning RPC over the unix socket.
        # A note for testing: this is set in instance initialization,
        # which means we can safely alter the config dynamically as we
        # create multiple instances.
        # For tests, we use an index to identify the paths:
        if configdata["lightningrpc-path"] == "regtest":
            self.clnrpc_socket_path = "/tmp/l" + str(
            LNOnionMessageChannel.test_index) + "-regtest/regtest/lightning-rpc"
            LNOnionMessageChannel.test_index += 1
        else:
            self.clnrpc_socket_path = configdata['lightningrpc-path']
        # hostid is a feature to avoid replay attacks across message channels;
        # TODO investigate, but for now, treat LN as one "server".
        self.hostid = "lightning-network"
        # keep track of peers. the list will be instances
        # of LNOnionPeer:
        self.peers = []
        print("dns are: ", configdata["directory-nodes"])
        for dn in configdata["directory-nodes"].split(","):
            # note we don't use a nick for directories:
            self.peers.append(LNOnionPeer.from_location_string(dn,
                                                directory=True))
        # the protocol factory for receiving TCP message for us:
        self.tcp_passthrough_factory = TCPPassThroughFactory()
        # -1 because we already incremented the global
        port = configdata["passthrough-port"]
        port_to_listen = port + LNOnionMessageChannel.test_index -1 if \
            configdata["lightningrpc-path"] == "regtest" else port
        reactor.listenTCP(port_to_listen, self.tcp_passthrough_factory)
        # will be needed to send messages:
        self.rpc_client = None

        # special "genesis" bootstrap case; there are no
        # directories but us.
        self.genesis_node = False


# ABC implementation section
    def run(self):
        self.rpc_client = LightningRpc(self.clnrpc_socket_path)
        # now the RPC is up, let's find out our own details,
        # so we can forward them to peers:
        self.get_our_peer_info()
        # Next, tell the server routing Lightning messages *to* us
        # that we're ready to listen:
        self.tcp_passthrough_factory.add_tcp_listener(self)
        # at this point the only peers added are directory
        # nodes from config, so we connect to all, and
        # after connecting, we retrieve the peer lists available:
        reactor.callLater(0.0, self.connect_to_directories)

    def get_pubmsg(self, msg, source_nick=None):
        """ Converts a message into the known format for
        pubmsgs; if we are not sending this (because we
        are a directory, forwarding it), `source_nick` must be set.
        Note that pubmsg does NOT prefix the *message* with COMMAND_PREFIX.
        """
        nick = source_nick if source_nick else self.nick
        return nick + COMMAND_PREFIX + "PUBLIC" + msg
 
    def get_privmsg(self, nick, cmd, message, source_nick=None):
        """ See `get_pubmsg` for comment on `source_nick`.
        """
        from_nick = source_nick if source_nick else self.nick
        return from_nick + COMMAND_PREFIX + nick + COMMAND_PREFIX + \
               cmd + " " + message

    def _pubmsg(self, msg):
        """ Best effort broadcast of message `msg`:
        send the message to every known directory node,
        with the PUBLIC message type and nick.
        """
        print("Pubmsging to: {}".format(msg))
        peerids = self.get_directory_peers()
        for peerid in peerids:
            self._send(peerid, LNOnionMessage(self.get_pubmsg(msg),
                                JM_MESSAGE_TYPES["pubmsg"]).encode())

    def _privmsg(self, nick, cmd, msg):
        print("Privmsging to: {}, {}, {}".format(nick, cmd, msg))
        encoded_privmsg = LNOnionMessage(self.get_privmsg(nick, cmd, msg),
                            JM_MESSAGE_TYPES["privmsg"]).encode()
        peerid = self.get_peerid_by_nick(nick)
        if not peerid:
            # If we are trying to message a peer via their nick, we
            # may not yet have connection info; then we just
            # forward via directory nodes.
            log.debug("Privmsg peer: {} but don't have peerid; "
                     "sending via directory.".format(nick))
            try:
                peerid = self.get_connected_directory_peers()[0].peerid
            except Exception as e:
                log.warn("Failed to send privmsg because no "
                         "directory peer is connected.")
                return
        self._send(peerid, encoded_privmsg)

    def _announce_orders(self, offerlist):
        pass
# End ABC implementation section


    def get_our_peer_info(self):
        """ Create a special LNOnionPeer object,
        outside of our peerlist, to refer to ourselves.
        """
        resp = self.rpc_client.call("getinfo")
        print("Got response from getinfo: ", resp)
        # See: https://lightning.readthedocs.io/lightning-getinfo.7.html
        # for the syntax of the response.
        #
        # TODO handle an error response.
        peerid = resp["id"]
        dp = self.get_directory_peers()
        self_dir = False
        if [peerid] == dp:
            print("this is the genesis node: ", peerid)
            self.genesis_node = True
            self_dir = True
        elif peerid in dp:
            # Here we are just one of many directory nodes,
            # which should be fine, we should just be careful
            # to not query ourselves.
            self_dir = True

        # TODO ; any obvious way to process multiple addresses,
        # other than just take the first?
        if len(resp["address"]) > 0:
            a = resp["address"][0]
        else:
            # special case regtest: we just use local, no
            # address, only binding:
            a = resp["binding"][0]
        addrtype = a["type"]
        if addrtype not in ["ipv4", "ipv6", "torv3"]:
            raise LNOnionPeerError("Unsupported internet address type: "
                                   "{}".format(addrtype))
        hostname = a["address"]
        port = a["port"]
        # TODO probably need to parse version, alias and network info
        self.self_as_peer = LNOnionPeer(peerid, hostname, port,
                                        self_dir, nick=self.nick)

    def connect_to_directories(self):
        """ First job of the bot is to get an
        up to date peer list (assuming it is not the seeding
        node).
        """
        if self.genesis_node:
            self.on_welcome(self)
            return
        for p in self.peers:
            print("the node: ", self.self_as_peer.peerid,
                  " is trying to connect to the node: ", p.peerid)
            p.connect(self.rpc_client)
        # after all the connections are in place, we can
        # start our continuous request for peer updates:
        peer_request_loop = task.LoopingCall(self.send_getpeers)
        peer_request_loop.start(10.0)
        # tell the joinmarketd daemon that we're ready
        # (we needed to wait until we had a fresh peer list).
        # This is what triggers the start of taker/maker workflows.
        self.on_welcome(self)

    def get_directory_peers(self):
        return [ p.peerid for p in self.peers if p.directory is True]

    def get_peerid_by_nick(self, nick):
        for p in self.get_all_connected_peers():
            if p.nick == nick:
                return p.peerid
        return None

    def _send(self, peerid: str, message: bytes) -> None:
        """
        This method is "raw" in that it only respects
        c-lightning's sendonionmessage syntax; it does
        not manage the syntax of the underlying Joinmarket
        message in any way.
        Sends a message to a peer on the message channel,
        identified by `peerid`, in TLV hex format.
        To encode the `message` field use `LNOnionMessage.encode`.
        """
        payload={
            "hops": [{"id": peerid,
                      "rawtlv": message}]
        }
        # TODO handle return:
        self.rpc_client.call("sendonionmessage", payload)

    def shutdown(self):
        """ TODO
        """

    def receive_msg(self, data):
        """ The entry point for all data coming over LN into our process.
        This includes control messages from the plugin that inform
        us about updates to peers. Notice that since the lightningd daemon
        doesn't know about our message types, it just lumps them all into
        "unknown_fields".
        """
        try:
            msgtypeval = data["unknown_fields"][0]
            msgtype = msgtypeval["number"]
            msgval = msgtypeval["value"]
        except Exception as e:
            log.warn("Ill formed message received: {}, exception: {}".format(
                data, e))
            return
        if msgtype in LOCAL_CONTROL_MESSAGE_TYPES.values():
            self.process_control_message(msgtype, msgval)
            # local control messages are processed first, as their "value"
            # field is not in the onion-TLV format.
            return
        # this converts the hex-encoded message from c-lightning
        # into JM's text string:
        msgval = LNOnionMessage.from_sendonionmessage_decode(msgval,
                                                    msgtype).text
        if self.process_control_message(msgtype, msgval):
            # will return True if it is, elsewise, a control message.
            return

        # ignore non-JM messages:
        if not msgtype in JM_MESSAGE_TYPES.values():
            log.debug("Invalid message type, ignoring: {}".format(msgtype))
            return

        # real JM message; should be: from_nick, to_nick, cmd, message
        try:
            nicks_msgs = msgval.split(COMMAND_PREFIX)
            from_nick, to_nick = nicks_msgs[:2]
            msg = COMMAND_PREFIX + COMMAND_PREFIX.join(nicks_msgs[2:])
            if to_nick == "PUBLIC":
                log.debug("A pubmsg is being processed by {} from {}; it "
                    "is {}".format(self.self_as_peer.nick, from_nick, msg))
                self.on_pubmsg(from_nick, msg)
                if self.self_as_peer.directory:
                    self.forward_pubmsg_to_peers(msg, from_nick)
            elif to_nick != self.nick:
                if not self.self_as_peer.directory:
                    log.debug("Ignoring message, not for us: {}".format(msg))
                else:
                    self.forward_privmsg_to_peer(to_nick, msg, from_nick)
            else:
                self.on_privmsg(from_nick, msg)
        except Exception as e:
            log.debug("Invalid joinmarket message: {}, error was: {}".format(msgval, repr(e)))
            return

    def forward_pubmsg_to_peers(self, msg, from_nick):
        """ Used by directory nodes currently. Takes a received
        message that was PUBLIC and broadcasts it to the non-directory
        peers.
        """
        assert self.self_as_peer.directory
        pubmsg = self.get_pubmsg(msg, source_nick=from_nick)
        msgtype = JM_MESSAGE_TYPES["pubmsg"]
        # TODO: specifically with forwarding/broadcasting,
        # we introduce the danger of infinite re-broadcast,
        # if there is more than one party forwarding.
        encoded_msg = LNOnionMessage(pubmsg, msgtype).encode()
        for peer in self.get_connected_nondirectory_peers():
            # don't loop back to the sender:
            if peer.nick == from_nick:
                continue
            log.debug("Sending {}:{} to nondir peer {}".format(msgtype, pubmsg, peer.peerid))
            self._send(peer.peerid, encoded_msg)

    def forward_privmsg_to_peer(self, nick, message, from_nick):
        assert self.self_as_peer.directory
        peerid = self.get_peerid_by_nick(nick)
        if not peerid:
            return
        # The `message` passed in has format COMMAND_PREFIX||command||" "||msg
        # we need to parse out cmd, message for sending.
        _, cmdmsg = message.split(COMMAND_PREFIX)
        cmdmsglist = cmdmsg.split(" ")
        cmd = cmdmsglist[0]
        msg = " ".join(cmdmsglist[1:])
        privmsg = self.get_privmsg(nick, cmd, msg, source_nick=from_nick)
        log.debug("Sending out privmsg: {} to peer: {}".format(privmsg, peerid))
        encoded_msg = LNOnionMessage(privmsg,
                        JM_MESSAGE_TYPES["privmsg"]).encode()
        self._send(peerid, encoded_msg)

    def process_control_message(self, msgtype: int, msgval: str) -> None:
        """ Triggered by a directory node feeding us
        peers, or by a connect/disconnect hook
        in the c-lightning plugin; this is our housekeeping
        to try to create, and keep track of, useful connections.
        """
        all_ctrl = list(LOCAL_CONTROL_MESSAGE_TYPES.values(
            )) + list(CONTROL_MESSAGE_TYPES.values())
        if msgtype not in all_ctrl:
            return False
        print("received control message: {},{}".format(msgtype, msgval))
        if msgtype == CONTROL_MESSAGE_TYPES["peerlist"]:
            # This is the base method of seeding connections;
            # a directory node can send this any time. We may well
            # need to control this; for now it just gets processed,
            # whereever it came from:
            try:
                peerlist = msgval.split(",")
                for peer in peerlist:
                    # defaults mean we just add the peer, not
                    # add or alter its connection status:
                    self.add_peer(peer, with_nick=True)
                return True
            except Exception as e:
                print("Incorrectly formatted peer list: {}, "
                      "ignoring, {}".format(msgval, e))
        elif msgtype == CONTROL_MESSAGE_TYPES["getpeerlist"]:
            # getpeerlist must be accompanied by a full node
            # locator, and nick;
            # add that peer before returning our peer list.
            p = self.add_peer(msgval, connection=True,
                              overwrite_connection=True, with_nick=True)
            self.send_peers(p)
            return True
        elif msgtype == LOCAL_CONTROL_MESSAGE_TYPES["connect"]:
            self.add_peer(msgval, connection=True,
                          overwrite_connection=True)
        elif msgtype == LOCAL_CONTROL_MESSAGE_TYPES["disconnect"]:
            self.add_peer(msgval, connection=False,
                          overwrite_connection=False)
        else:
            assert False

    def get_peer_by_id(self, p: str):
        """ Returns the LNOnionPeer with peerid p,
        if it is in self.peers, otherwise returns False.
        """
        for x in self.peers:
            if x.peerid == p:
                return x
        return False

    def add_peer(self, peerdata: str, connection: bool=False,
                overwrite_connection: bool=False, with_nick=False) -> None:
        """ add non-directory peer from (nick, peer) serialization `peerdata`,
        where "peer" is peerid or full peerid@host:port;
        return the created LNOnionPeer object. Or, with_nick=False means
        that `peerdata` has only the peer location.
        If the peer is already in our peerlist it can be updated in
        one of these ways:
        * the nick can be added
        * it can be marked as 'connected' if it was previously unconnected,
        with this conditional on whether the flag `overwrite_connection` is
        set.
        """
        if with_nick:
            try:
                nick, peer = peerdata.split(NICK_PEERLOCATOR_SEPARATOR)
            except Exception as e:
                # TODO: as of now, this is not an error, but expected.
                # Don't log? Do something else?
                log.debug("Received invalid peer identifier string: {}, {}".format(
                    peerdata, e))
                return
        else:
            peer = peerdata
        if len(peer) == 66:
            p = self.get_peer_by_id(peer)
            if not p:
                # no address info here
                p = LNOnionPeer(peer)
                p.is_connected = connection
                self.peers.append(p)
            elif overwrite_connection:
                p.is_connected = connection
            if with_nick:
                p.set_nick(nick)
            return p
        elif len(peer) > 66:
            # assumed that it's passing a full string
            # TODO need to think about logic of 'is_connected'
            # state here.
            temp_p = LNOnionPeer.from_location_string(peer)
            if not self.get_peer_by_id(temp_p.peerid):
                temp_p.is_connected = connection
                if with_nick:
                    temp_p.set_nick(nick)
                self.peers.append(temp_p)
                return temp_p
            else:
                p = self.get_peer_by_id(temp_p.peerid)
                if connection and overwrite_connection:
                    p.is_connected = True
                if with_nick:
                    p.set_nick(nick)
                return p
        else:
            raise LNOnionPeerError(
            "Invalid peer location string: {}".format(peer))

    def get_all_connected_peers(self):
        return self.get_connected_directory_peers() + \
               self.get_connected_nondirectory_peers()

    def get_connected_directory_peers(self):
        return [p for p in self.peers if p.directory and p.is_connected]

    def get_connected_nondirectory_peers(self):
        return [p for p in self.peers if (not p.directory) and p.is_connected]

    """ CONTROL MESSAGES SENT BY US
    """
    def send_getpeers(self):
        """ This message is sent to all currently connected
        directory nodes.
        """
        for dp in self.get_connected_directory_peers():
            # This message embeds the connection information
            # for *ourselves* to add to peer lists of other
            # nodes.
            msg = self.self_as_peer.get_nick_peerlocation_ser()
            self._send(dp.peerid, LNOnionMessage(msg,
                        CONTROL_MESSAGE_TYPES["getpeerlist"]).encode())

    def send_peers(self, requesting_peer):
        """ This message is sent by directory peers on request
        by non-directory peers.
        The peerlist message should have this format:
        (1) entries comma separated
        (2) each entry is serialized nick then the NICK_PEERLOCATOR_SEPARATOR
            then *either* 66 char hex peerid, *or* peerid@host:port
        (3) However since it's very likely that this message
            is long enough to exceed a 1300 byte limit, we
            must split it into multiple messages (TODO).
        """
        if not requesting_peer.is_connected:
            raise LNOnionPeerConnectionError(
                "Cannot send peer list to unconnected peer")
        peerlist = []
        for p in self.get_connected_nondirectory_peers():
            # don't send a peer to itself
            if p.peerid == requesting_peer.peerid:
                continue
            if not p.is_connected:
                # don't advertise what is not online (?)
                continue
            # peers that haven't sent their nick yet are not
            # privmsg-reachable; don't send them
            if p.nick == "":
                continue
            peerlist.append(p.get_nick_peerlocation_ser())
        self._send(requesting_peer.peerid, LNOnionMessage(",".join(
            peerlist), CONTROL_MESSAGE_TYPES["peerlist"]).encode())


