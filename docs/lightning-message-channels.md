# HOW TO SETUP LIGHTNING MESSAGE CHANNELS IN JOINMARKET

**WORK IN PROGRESS: currently only on test networks.**

### Contents

1. [Purpose](#purpose)

2. [Installing c-lightning](#install-ln)

3. [Configure and run on regtest](#regtest)

4. [Configure for signet](#signet)

<a name="purpose" />

## Purpose

The discussion of "more decentralized than just using IRC servers", for Joinmarket, has been ongoing for years and in particular the most recent extended discussion is to be found in [this issue thread](https://github.com/JoinMarket-Org/joinmarket-clientserver/issues/415).

Why Lightning and not just Tor? We gain a few things:

* We can make connections between messaging and payments, or between actual coinjoins and Lightning payments. Neither of these probably works in the *simplest* way imaginable, but with a little sophistication this could be *tremendously* powerful (an obvious example: directory nodes could charge for their service on a per-message basis, not using Lightning payments inside the message flow, which is too slow, but based on Chaumian token issuance against earlier Lightning payments).
* By using a plugin to a c-lightning node, we leverage the high quality, high performance of their C code to do a lot of heavy lifting. Passing messages over hops, using onion routing, is intrinsic to what their codebase has to provide (including, over Tor), so if we use the `sendonionmessage` between our nodes, we make use of that benefit.
* There is an overlap in concept between *coinjoins* and *dual funding* in Lightning, since technically the latter *is* a class of the former. A natural question arises, hopefully one that can be answered over time: can we integrate Joinmarket style coinjoins into the dual funding process, or perhaps also single funding (some of this interacts with taproot, of course, so it's not a simple matter). Also related is recent work by @niftynei on "liquidity advertising", see e.g. [here](https://medium.com/blockstream/setting-up-liquidity-ads-in-c-lightning-54e4c59c091d).

(c-lightning's [plugin architecture](https://lightning.readthedocs.io/PLUGINS.html) is a big part of what makes this relatively easy to implement; it means we have a very loose coupling via a simple plugin that forwards messages and notifications from the c-lightning node to joinmarket's jmdaemon, which is able to use it as "just another way of passing messages", basically).

What is in this PR *so far* as of 31 Aug 2021, time of writing, is limited, but functional. The directory nodes are queried for connections (after being connected to, of course), to give takers a view of the current trading pit. They also basically do all the heavy lifting, broadcasting `pubmsgs` and are used to forward `privmsgs` also (except in cases where participants already have an existing connection). This will be changed at some point to use onion routing with hops, which is already included in the `sendonionmessage` API, see [here](https://lightning.readthedocs.io/lightning-sendonionmessage.7.html) for details - notice the `hops` array. I say *will be changed*, because it's really needed if we want to gain the scale that such a more decentralized approach can achieve.


<a name="install-ln" />

## C-Lightning installation.

Obviously it's a disadvantage to require an additional piece of software (if you're not already running it, or if you're running lnd), but there are two ameliorating factors:

* You don't, for now, need to even fund the wallet or use anything in the software apart from running the Joinmarket plugin.

* It's actually very easy to build and install on Linux.

The installation instructions are on the repo, however there is a very important detail before you go there, and follow those instructions. You unfortunately *must* compile and build, not just install the binaries, because `sendonionmessage` is still in the "experimental features" set, and you must run:

```
./configure --enable-developer --enable-experimental-features`
```

and not just

```
./configure
```

With that said, follow the guide for building on your Linux distro [here](https://github.com/ElementsProject/lightning/blob/master/doc/INSTALL.md).


<a name="regtest" />

## Running tests on regtest

Since this doc (and code) are in a testing phase, you will want to run on either regtest or signet to start. Here there's an especially good reason to get it running on regtest: c-lightning has a purpose built environment to make this very easy. Once you've completed the installation instructions, and assuming you have `bitcoind` and `bitcoin-cli` in your path, you'll be able to:

```
cd contrib/
source ./startup_regtest.sh
start_ln 3
```

(I recommend reading the detailed explanation in the comments of the `startup_regtest.sh` script [here](https://github.com/ElementsProject/lightning/blob/77d2c538b3ca0c546d15d4f505cab33d44cfb07f/contrib/startup_regtest.sh#L3-L31)). From here you already have a working "Lightning-network-in-a-box" with 3 nodes ready to go.

Once you've done this once and know that it works, you're ready to test Joinmarket with it. First, you need to edit that startup_regtest.sh script to include the Joinmarket plugin configuration. Change:

```
test -f "/tmp/l$i-$network/lightningd-$network.pid" || \ "$LIGHTNINGD" "--lightning-dir=/tmp/l$i-$network" &
```

to:

```
test -f "/tmp/l$i-$network/lightningd-$network.pid" || \
                        "$LIGHTNINGD" "--dev-force-privkey=121212121212121212121212121212121212121212121212121212121212121$i" "--plugin=/path/to/joinmarket-clientserver/jmdaemon/jmdaemon/jmcl.py" "--jmport=4910$i" "--lightning-dir=/tmp/l$i-$network" &
```

the weird `--dev-force-privkey` parameter is not necessary, but it's very helpful in tests, since it means every node will always have the same pubkey from run to run. The other two added parameters should be self-explanatory.

Small note: c-lightning does *not* need to be running in the `(jmvenv)` virtualenv that we use for Joinmarket; but you do need to install twisted (e.g. `pip3 install twisted`).

After re-running the above c-lightning regtest setup with this edited startup file, but with 4 nodes rather than 3 (i.e. change to `start-ln 4`), you can now run the normal Joinmarket regtest setup file `ygrunner.py`, but first you need to edit the Joinmarket config file, which for testing is in current working directory. To the normal regtest version of `joinmarket.cfg`, make this edit:

```
[MESSAGING:lightning1]
type = ln-onion
# This is a comma separated list (comma can be omitted if only one item).
# Each item has format pubkey@host:port ; all are required. Host can be
# a *.onion address (tor v3 only).
directory-nodes = 03df15dbd9e20c811cc5f4155745e89540a0b83f33978317cebe9dfc46c5253c55@localhost:7171
# note that this setting in particular needs dynamic editing in tests of multiple
# nodes on one machine and this is marked with the special string 'regtest',
# but for normal running it is just located in your ~/.lightning:
lightningrpc-path = regtest
passthrough-port = 49100
```

(and, comment out or delete the IRC message channel entries). How this differs from what's placed currently in `configure.py` is the "regtest" setting for `lightning-rpc`, and it is explained in the comment above it; it enables *the multiple joinmarket bots started together in ygrunner.py to connect to the separate backend c-lightning nodes that were started up with start-ln*. Note that *if* you use the exact same `--dev-force-privkey` setting as explained above, then the entry for `directory-nodes` here will work correctly: it'll be the first c-lightning node, whose log is in `/tmp/l1-regtest/log`.

Once you've made this config change you're ready to:

```
(jmvenv) a@b/joinmarket-clientserver$ pytest --btcconf=/path/to/bitcoin.conf --btcroot=/path/to/bitcoin/bin/ --btcpwd=123456abcdef --nirc=2 test/ygrunner.py -s -p no:warnings
```
See the [testing doc](TESTING.md); you are running this test setup just as usual, with that config change. Make sure you reach `JM daemon setup complete` which as usual means that the message channel(s) are up/ready.

To do a transaction, then as usual start a new terminal, set up the virtualenv, go to the scripts/ directory, copy the above `joinmarket.cfg` into there, but edit the `lightningrpc-path` setting to be `tmp/l4-regtest/regtest/lightning-rpc` - this is the Unix socket that Joinmarket uses to make RPC calls to c-lightning, and we're using the 4th instance because the `ygrunner` script made use of the first 3, for the makers.

Then do a `sendpayment` as usual to see it working.


<a name="signet" />

## Configure for signet.

Edit your `joinmarket.cfg` (in this case, probably in `~/.joinmarket/joinmarket.cfg`  unless you use `--datadir`). Add the same Lightning config as above, but you need to specify a directory node, which *can* be your own node (then it won't do much, but you can give it to others).

```
[MESSAGING:lightning1]
type = ln-onion
# This is a comma separated list (comma can be omitted if only one item).
# Each item has format pubkey@host:port ; all are required. Host can be
# a *.onion address (tor v3 only).
directory-nodes = somepubkey@somehost:someport
# note that this setting in particular needs dynamic editing in tests of multiple
# nodes on one machine and this is marked with the special string 'regtest',
# but for normal running it is just located in your ~/.lightning:
lightningrpc-path = ~/.lightning/signet/lightning-rpc
passthrough-port = 49100
```

Notice that the lightningrpc-path is changed.

If we're going to run a "real" instance on signet, we just need to start up one c-lightning node:

```
lightningd --signet --daemon --plugin=/path/to/joinmarket-clientserver/jmdaemon/jmdaemon/jmcl.py --jmport=49100
```

As far as I know this won't be doing much without you choosing to fund, connect, open channels etc. But this should
allow you to now do normal Joinmarket operations with peers that have at least one directory node in common.
