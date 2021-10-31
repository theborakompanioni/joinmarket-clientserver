from jmbase import bintohex
mock_getinfo_result = {'id': '03df15dbd9e20c811cc5f4155745e89540a0b83f33978317cebe9dfc46c5253c55',
                       'alias': 'BIZARREYARD-v0.9.1-13-gc8c2227', 'color': '028984', 'num_peers': 0,
                       'num_pending_channels': 0, 'num_active_channels': 0, 'num_inactive_channels': 0,
                       'address': [], 'binding': [{'type': 'ipv4', 'address': '127.0.0.1', 'port': 9835}],
                       'version': 'v0.9.1-13-gc8c2227', 'blockheight': 61988, 'network': 'regtest',
                       'msatoshi_fees_collected': 0, 'fees_collected_msat': "0msat", 'lightning-dir': '/not/real/path'}
mock_control_message1 = {"unknown_fields": [{"number": 789,
                        "value": bintohex(b"ln-receiver;028984b787834f93dbac6b9902368cdc2da34c563cb5626a484109f35aac32e84e")}]}