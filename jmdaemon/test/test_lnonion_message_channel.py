#! /usr/bin/env python
'''Tests of LNOnionMessageChannel function '''

import time
import json
import copy
from twisted.trial import unittest
from twisted.internet import reactor, defer
from jmdaemon import LNOnionMessageChannel, MessageChannelCollection, COMMAND_PREFIX
from jmclient import (load_test_config, get_mchannels, jm_single)
from jmdaemon.lnonion import TCPPassThroughProtocol
from jmbase import get_log
from ln_test_data import mock_getinfo_result, mock_control_message1

""" We need to spoof both incoming and outcoming.
Incoming is from lightningd -> sendonionmessage trigger -> jmcl plugin -> tcp-passthrough
Outgoing is from here to lightningd via LightningRpc.
Creating a full test of the lightningd backend, which necessarily requires multiple nodes,
is a large project, so here we are less ambitious. We make a pretend version of the
LightningRpc client which stores the received messages, indexed by nick.
Then that same object sends back the messages in tcp-passthrough messages.
"""

log = get_log()

nick1 = "ln_publisher"
nick2 = "ln_receiver"
nick3 = "ln_thirdparty"

class DummyLightningBackend(TCPPassThroughProtocol):
    connected_nodes = set()

    def send_message(self, message_object) -> None:
        self.sendLine(json.dumps(message_object).encode("utf-8"))

    def lineReceived(self, line):
        """ The delta to the base class is, we need to decide
        here who is to receive the message (in jmcl.py we just forward
        everything, because LN has already routed by nodeid)
        , which means we need to parse out the nick/node identifiers.
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

    def call(self, method, data):
        """ We spoof the outgoing LightningRpc calls
        `sendonionmessage`, `connect/disconnect` and `getinfo`, using the crude db
        stored by this protocol instance.
        """
        if method == "getinfo":
            self.send_message({"testkey": "test string return of getinfo"})
        elif method == "connect":
            self.add_connected_node(data)
        elif method == "disconnect":
            self.remove_connected_node(data)
        elif method == "sendonionmessage":
            # TODO format
            # payload={
            #"hops": [{"id": peerid,
            #          "rawtlv": message}]
            #}
            self.send_message(data)
        else:
            assert False

    def add_connected_node(self, data):
        self.connected_nodes.add(data["nodeid"])

    def remove_connected_node(self, data):
        self.connected_nodes.remove(data["nodeid"])

si = 0.1

# The "daemon" here is specifically the daemon protocol in
# jmdaemon; we just make sure that the usual nick signature
# verification alawys passes:
class DummyDaemon(object):
    def request_signature_verify(self, a, b, c, d, e,
            f, g, h):
        return True

# wrapper class allows us to set a nick
# initially without jmclient interaction:
class DummyMC(LNOnionMessageChannel):
    def __init__(self, configdata, nick, daemon):
        super().__init__(configdata, daemon=daemon)
        self.daemon = daemon
        self.set_nick(nick)

    def get_rpc_client(self, path):
        return DummyRpcClient(path)

class DummyRpcClient(object):
    def __init__(self, path):
        self.path = path

    def call(self, method, obj=None):
        "Call of method {} with data {} occurred in DummyRpcClient".format(method, obj)
        if method == "getinfo":
            return mock_getinfo_result
        else:
            return {}

def on_connect(x):
    print('simulated on-connect')

@defer.inlineCallbacks
def on_welcome(mc):
    print('simulated on-welcome')
    yield junk_pubmsgs(mc)
    yield junk_longmsgs(mc)
    yield junk_announce
    yield junk_fill

def on_disconnect(x):
    print('simulated on-disconnect')

def on_order_seen(dummy, counterparty, oid, ordertype, minsize,
                                           maxsize, txfee, cjfee):
    global yg_name
    yg_name = counterparty

def on_pubkey(pubkey):
    print("received pubkey: " + pubkey)

def junk_pubmsgs(mc):
    mc.request_orderbook()
    #now try directly
    mc.pubmsg("!orderbook")
    #should be ignored; can we check?
    mc.pubmsg("!orderbook!orderbook")

def junk_longmsgs(mc):
    # TODO: not currently using real `sendonionmessage`,
    # so testing a longer than expected message is pointless.
    # in future mock or use the real lightning rpc call.
    # Leaving here for now as it doesn't hurt.
    mc.pubmsg("junk and crap"*40)

def junk_announce(mc):
    #try a long order announcement in public
    #because we don't want to build a real orderbook,
    #TODO: how to test that the sent format was correct?
    mc._announce_orders(["!abc def gh 0001"]*30)

def junk_fill(mc):
    cpname = "irc_receiver"
    #send a fill with an invalid pubkey to the existing yg;
    #this should trigger a NaclError but should NOT kill it.
    mc._privmsg(cpname, "fill", "0 10000000 abcdef")
    #Try with ob flag
    mc._pubmsg("!reloffer stuff")
    time.sleep(si)
    #Trigger throttling with large messages
    mc._privmsg(cpname, "tx", "aa"*5000)
    time.sleep(si)
    #with pytest.raises(CJPeerError) as e_info:
    mc.send_error(cpname, "fly you fools!")
    time.sleep(si)
    return mc

def getmc(nick):
    dm = DummyDaemon()
    mc = DummyMC(get_mchannels()[0], nick, dm)
    mc.register_orderbookwatch_callbacks(on_order_seen=on_order_seen)
    mc.register_taker_callbacks(on_pubkey=on_pubkey)
    mc.on_connect = on_connect
    mc.on_disconnect = on_disconnect
    mc.on_welcome = on_welcome
    mcc = MessageChannelCollection([mc])
    return dm, mc, mcc

class LNOnionTest(unittest.TestCase):

    def setUp(self):
        # add a config section dynamically, specifically
        # for this purpose:
        load_test_config()
        self.old_config = copy.deepcopy(jm_single().config)
        jm_single().config.remove_section("MESSAGING:server1")
        jm_single().config.remove_section("MESSAGING:server2")
        jm_single().config.add_section("MESSAGING:lightning1")
        
        jm_single().config.set("MESSAGING:lightning1", "type", "ln-onion")
        jm_single().config.set("MESSAGING:lightning1", "directory-nodes",
            "03df15dbd9e20c811cc5f4155745e89540a0b83f33978317cebe9dfc46c5253c55@127.0.0.1:9835")
        jm_single().config.set("MESSAGING:lightning1", "passthrough-port", str(49100))
        jm_single().config.set("MESSAGING:lightning1", "lightning-port", "9835")
        jm_single().config.set("MESSAGING:lightning1", "lightning-rpc", "mock-rpc-socket-file")
        print(get_mchannels()[0])
        jm_single().maker_timeout_sec = 1
        self.dm, self.mc, self.mcc = getmc("irc_publisher")
        self.mcc.run()
        print("Got here")

    def test_all(self):
        # it's slightly simpler to test each functionality in series
        # in one test function (so we can preserve order).
        self.mc.on_welcome(self.mc)
        tm = "testmessage"
        assert self.mc.get_pubmsg(tm, source_nick=nick1) == \
               nick1 + COMMAND_PREFIX + "PUBLIC" + tm
        assert self.mc.get_privmsg(nick2, "ioauth", tm,
                source_nick=nick1) == nick1 + COMMAND_PREFIX + \
               nick2 + COMMAND_PREFIX + "ioauth " + tm
        # now test the same messages using the full MC abstraction layer.
        # unfortunately these are just "no crashing" tests for now,
        # since we are not verifying reception anywhere:
        self.mcc.pubmsg(tm)
        # in order for us to privmsg a counterparty we need to think
        # it's visible; to do that we mock a control message from the dn
        # telling us that he's there:
        self.mc.receive_msg(mock_control_message1)
        # absence of 'peer not found' in response to this
        # privmsg will mean the above worked:
        self.mcc.privmsg(nick2, "ioauth", tm)
        # check our peerinfo is right:
        sap = self.mc.self_as_peer       
        assert sap.peerid == '03df15dbd9e20c811cc5f4155745e89540a0b83f33978317cebe9dfc46c5253c55'
        assert sap.hostname == '127.0.0.1'
        assert sap.port == 9835

    def tearDown(self):
        jm_single().config = self.old_config
        for dc in reactor.getDelayedCalls():
            dc.cancel()
        # only fire if everything is finished:
        return defer.maybeDeferred(self.mc.tcp_passthrough_listener.stopListening)






