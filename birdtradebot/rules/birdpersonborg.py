rules = [
   {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHBTC" in {tweet} and "long" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [ 
            {
                'side': 'buy',
                'type': 'limit',
                'price': '{inside_bid}',
                'product_id': 'ETH-BTC',
                'size': '{max_balance}'
            }
        ]
   },
   {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHBTC" in {tweet} and "short" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [ 
            {
                'side': 'sell',
                'type': 'limit',
                'price': '{inside_ask}',
                'product_id': 'ETH-BTC',
                'size': '{available[ETH]}'
            }
        ]
   },
   {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHUSD" in {tweet} and "long" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [ 
            {
                'side': 'buy',
                'type': 'limit',
                'price': '{inside_bid}',
                'product_id': 'ETH-EUR',
                'size': '{max_balance}'
            }
        ]
    },
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"ETHUSD" in {tweet} and "short" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [ 
            {
                'side': 'sell',
                'type': 'limit',
                'price': '{inside_ask}',
                'product_id': 'ETH-EUR',
                'size': '{available[ETH]}'
            }
        ]
    },
   {
        'handles': ['BirdpersonBorg'],
        'condition': '"BTCEUR" in {tweet} and "long" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [
            {
                'side': 'buy',
                'type': 'limit',
                'price': '{inside_bid}',
                'product_id': 'BTC-EUR',
                'size': '{max_balance}'
            }
        ]
    },
    {
        'handles': ['BirdpersonBorg'],
        'condition': '"BTCEUR" in {tweet} and "short" in {tweet}.lower()',
        'retries': 1,
        'retry_ttl_s': 300,
        'tweet_ttl_s': 600,
        'market_fallback': True,
        'orders': [
            {
                'side': 'sell',
                'type': 'limit',
                'price': '{inside_ask}',
                'product_id': 'BTC-EUR',
                'size': '{available[BTC]}'
            }
        ]
    }
]
