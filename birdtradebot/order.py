import time
from typing import Dict, List, Union

from .utils import D

order_vocab = frozenset([
    'client_oid', 'type', 'side', 'product_id', 'stp',
    'price', 'size', 'time_in_force', 'cancel_after',
    'post_only', 'funds', 'overdraft_enabled', 'funding_amount',
])


class OrderError(Exception):
    pass


class OrderSizeTooSmall(OrderError):
    pass


class InsufficientFunds(OrderError):
    pass


class OrderNotFound(OrderError):
    pass


class OrderExpired(OrderError):
    pass


class Order:
    def __init__(self, order_dict: Dict[str, str]):
        self.order_dict = order_dict
        self.type = order_dict.get('type', 'limit')
        self.time_in_force = order_dict.get('time_in_force')
        self.post_only = order_dict.get('post_only', False)
        self.side = order_dict.get('side')
        self.product_id = order_dict.get('product_id')
        self.price = D(order_dict.get('price'))
        self.size = D(order_dict.get('size'))
        self.funds = D(order_dict.get('funds'))
        self.error = order_dict.get('error')
        self.raw_server_reply = None

    def _validate(self):
        '''Validate order; follow https://docs.gdax.com/#orders for
        filling in default values.'''

        unrecognized_keys = [
            key for key in self.order_dict if key not in order_vocab
        ]

        if unrecognized_keys:
            raise ValueError(
                'The order keys: %s in order %s are invalid.' %
                (unrecognized_keys, self.order_dict)
            )
        if self.type not in ('limit', 'market', 'stop'):
            raise ValueError(
                'Invalid order: "type" must be one of "limit", "market" '
                'or "stop". Order is: %s' % self.order_dict
            )

        if self.side not in ['buy', 'sell']:
            raise ValueError(
                'An order "side" must be one of "buy" or "sell". Order is %s' %
                self.order_dict
            )

        if self.product_id is None:
            raise ValueError(
                'An order must have a "product_id", but this order does not: %s'
                % self.order_dict
            )

        if self.type == 'limit':
            if self.price is None or self.size is None:
                raise ValueError(
                    'If an order "type" is "limit", it must specify both a '
                    '"size" and "price". This order does not: %s' % self.order_dict
                )
        elif self.type in ['market', 'stop']:
            if self.size is None and self.funds is None:
                raise ValueError(
                    'If an order "type" is %s, it must specify on of '
                    '"size" or "funds". This order does not: %s' %
                    (self.type, self.order_dict)
                )

        if self.time_in_force not in (None, 'GTC', 'FOK'):
            raise ValueError(
                "Only GTC and FOK limit order types are supported: %s",
                self.order_dict
            )

        for stack in ['size', 'funds', 'price']:
            try:
                attr = getattr(self, stack)
                if attr is None:
                    continue
                eval(attr).format(
                    tweet=('"The rain in Spain stays mainly in the plain."'),
                    available={
                        'ETH': .01,
                        'USD': .01,
                        'LTC': .01,
                        'BTC': .01
                    }, inside_bid=200, inside_ask=200)
            except Exception:
                raise ValueError(
                    '"%s from order %s could not be evaluated. Check the format '
                    "and try again" % (stack, self.order_dict)
                )


class OrderState(Order):
    def __init__(self, state: Dict[str, str]):
        super().__init__(state)
        self.id = state['id']
        self.filled_size = D(state['filled_size'])
        self.fill_fees = D(state.get('fill_fees', '0.0'))
        self.status = state['status']
        self.settled = state['settled']
        self.created_at = state['created_at']
        self.price = D(state.get('price', '0.0'))
        self.executed_value = D(state.get('executed_value', '0.0'))
        self.funds = D(self.funds)
        self.timestamp = int(time.time())


class OrderTemplate(Order):
    def __init__(self, config: Dict[str, str]):
        super().__init__(config)


class OrderBatch:
    def __init__(self):
        self.new: List[Order] = []
        self.done: List[OrderState] = []
        self.pending: List[OrderState] = []
        self.error: List[Union[OrderState, Order]] = []


def order_to_dict(order: Order, strict=False) -> Dict[str, str]:
    r = {}
    for f in order.__dict__.keys():
        if strict and f not in order_vocab:
            continue
        value = getattr(order, f, None)
        if value is not None:
            r[f] = str(value)

    return r


def dict_to_order(order_dict: Dict[str, str]) -> Order:
    return Order(order_dict)


def dict_to_order_state(order_state_dict: Dict[str, str]) -> OrderState:
    return OrderState(order_state_dict)
