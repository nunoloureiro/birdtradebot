eth_rules = [
   {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHEUR" in {tweet} and "long" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'order': {
            'side': 'buy',
            'type': 'limit',
            'price': '{inside_bid}',
            'product_id': 'ETH-EUR',
            'size': '{max_balance}'
        }
    },
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHEUR" in {tweet} and "short" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'order': {
            'side': 'sell',
            'type': 'limit',
            'price': '{inside_ask}',
            'product_id': 'ETH-EUR',
            'size': '{available[ETH]}'
        }
    },
]

gdax_exchange = {
    'type': 'gdax'
}

bitfinex_exchange = {
    'type': 'bitfinex'
}

accounts = {
    'account1': {
        'exchange': gdax_exchange,
        'balance': {'EUR': 0, 'ETH': 32},
        'rules': eth_rules,
    },
    'account2': {
        'exchange': bitfinex_exchange,
        'balance': {'USD': 0, 'IOT': 1000}
    }
}
