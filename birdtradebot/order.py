from typing import Dict

order_vocab = frozenset([
    'client_oid', 'type', 'side', 'product_id', 'stp',
    'price', 'size', 'time_in_force', 'cancel_after',
    'post_only', 'funds', 'overdraft_enabled', 'funding_amount',
])


class AbstractOrder:
    def __init__(self, config: Dict[str, str]):
        self.config = config
        self.type = config.get('type', 'limit')
        self.side = config.get('side')
        self.product_id = config.get('product_id')
        self.price = config.get('price')
        self.size = config.get('size')
        self.funds = config.get('funds')

    def _validate(self):
        '''Validate order; follow https://docs.gdax.com/#orders for
        filling in default values.'''

        unrecognized_keys = [
            key for key in self.config if key not in order_vocab
        ]

        if unrecognized_keys:
            raise RuntimeError(
                'The order keys: %s in order %s are invalid.' %
                (unrecognized_keys, self.config)
            )
        if self.type not in ('limit', 'market', 'stop'):
            raise RuntimeError(
                'Invalid order: "type" must be one of "limit", "market" '
                'or "stop". Order is: %s' % self.config
                )

        if self.side not in ['buy', 'sell']:
            raise RuntimeError(
                'An order "side" must be one of "buy" or "sell". Order is %s' %
                self.config
            )

        if self.product_id is None:
            raise RuntimeError(
                'An order must have a "product_id", but this order does not: %s'
                % self.config
            )

        if self.type == 'limit':
            if self.price is None or self.size is None:
                raise RuntimeError(
                    'If an order "type" is "limit", it must specify both a '
                    '"size" and "price". This order does not: %s' % self.config
                )
        elif self.type in ['market', 'stop']:
            if self.size is None and self.funds is None:
                raise RuntimeError(
                    'If an order "type" is %s, it must specify on of '
                    '"size" or "funds". This order does not: %s' %
                    (self.type, self.config)
                )

        for stack in ['size', 'funds', 'price']:
            try:
                eval(getattr(self, stack).format(
                    tweet=('"The rain in Spain stays mainly '
                           'in the plain."'),
                    available={
                        'ETH': .01,
                        'USD': .01,
                        'LTC': .01,
                        'BTC': .01
                    }, inside_bid=200, inside_ask=200))
            except KeyError:
                pass
            except Exception:
                raise RuntimeError(
                    '"%s from order %s could not be evaluated. Check the format '
                    "and try again" % (stack, self.config)
                )


class Order(AbstractOrder):
    def __init__(self, config: Dict[str, str]):
        super().__init__(config)


class OrderTemplate(AbstractOrder):
    def __init__(self, config: Dict[str, str]):
        super().__init__(config)


def order_to_dict(order: AbstractOrder) -> Dict[str, str]:
    r = {}
    for f in order.__dict__.keys():
        value = getattr(order, f, None)
        if value is not None:
            r[f] = value

    return r


def dict_to_order(order_dict: Dict[str, str]) -> AbstractOrder:
    return Order(order_dict)
