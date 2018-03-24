from copy import deepcopy

# Twitter
twitter = {
    'app_key': '',
    'app_secret': '',
    'oauth_token': '',
    'oauth_token_secret': '',
}

# Exchanges
exchanges = {
    'gdax': {
        'type': 'gdax',
        'key': '',
        'secret': '',
        'passphrase': '',
    },
    'bitfinex': {
        'type': 'bitfinex',
        'key': '',
        'secret': '',
        'passphrase': None,
    },
}

# Rules
bird_eth_rules = [
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHEUR" in {tweet} and "long" in {tweet}.lower()',
        'ttl': 300,
        'order_ttl': 60,
        'tweet_ttl': 600,
        'market_fallback': True,
        'position': 'long',
        'id': 'bird|ETH-EUR',
        'order': {
            'side': 'buy',
            'type': 'limit',
            'price': '{inside_bid}',
            'product_id': 'ETH-EUR',
            'size': '{max_account_buy_size}',
        }
    },
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHEUR" in {tweet} and "short" in {tweet}.lower()',
        'ttl': 300,
        'order_ttl': 60,
        'tweet_ttl': 600,
        'market_fallback': True,
        'position': 'short',
        'id': 'bird|ETH-EUR',
        'order': {
            'side': 'sell',
            'type': 'limit',
            'price': '{inside_ask}',
            'product_id': 'ETH-EUR',
            'size': '{max_account_sell_size}'
        }
    },
]

# Same rules but following a different account
slow_bird_eth_rules = deepcopy(bird_eth_rules)
for rule in slow_bird_eth_rules:
    rule['handles'] = ['SlowBirdperson']
    rule['id'] = 'slow-bird|ETH-EUR'


# Accounts
accounts = {
    'bird_eth_gdax': {
        'exchange': 'gdax',
        'initial_balance': {'EUR': 0, 'ETH': 0.1},
        'rules': bird_eth_rules,
    },
    'bird_eth_bitfinex': {
        'exchange': 'bitfinex',
        'initial_balance': {'EUR': 0, 'ETH': 0.1},
        'rules': bird_eth_rules,
    },
    'slow_bird_eth_bitfinex': {
        'exchange': 'bitfinex',
        'initial_balance': {'EUR': 0, 'ETH': 0.1},
        'rules': slow_bird_eth_rules,
    },
}

try:
    from local_config import *
except ImportError:
    pass