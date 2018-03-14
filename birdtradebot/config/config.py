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
    'exchange1': {
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
        'ttl': 600,
        'order_ttl': 60,
        'max_quote_currency': 7000,
        'tweet_ttl': 600,
        'market_fallback': True,
        'order': {
            'side': 'buy',
            'type': 'limit',
            'price': '{inside_bid}',
            'product_id': 'ETH-EUR',
            'size': '{max_size}',
        }
    },
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHEUR" in {tweet} and "short" in {tweet}.lower()',
        'ttl': 600,
        'order_ttl': 60,
        'max_quote_currency': 7000,
        'tweet_ttl': 600,
        'market_fallback': True,
        'order': {
            'side': 'sell',
            'type': 'limit',
            'price': '{inside_ask}',
            'product_id': 'ETH-EUR',
            'size': '{balance[ETH]}'
        }
    },
]

# Same rules but following a different account
slow_bird_eth_rules = deepcopy(bird_eth_rules)
for rule in slow_bird_eth_rules:
    rule['handles'] = ['SlowBirdperson']

# Accounts
accounts = {
    'account1': {
        'exchange': 'exchange1',
        'initial_balance': {'EUR': 0, 'ETH': 32},
        'rules': bird_eth_rules,
    },
    'account2': {
        'exchange': 'exchange1',
        'initial_balance': {'EUR': 0, 'ETH': 32},
        'rules': slow_bird_eth_rules,
    },

}

try:
    from local_config import *
except ImportError:
    pass