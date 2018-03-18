import datetime
import logging
import threading
import time
from decimal import Decimal
from typing import Dict, List, Set, Union, Callable

import gdax

from .exchanges import bitfinex
from .order import (
    Order,
    OrderState,
    OrderError,
    InsufficientFunds,
    OrderSizeTooSmall,
    OrderNotFound,
    order_to_dict)
from .rule import Rule
from .twitter import Tweet
from .utils import round_down, D

log = logging.getLogger(__name__)

StrOrNone = Union[str, None]
GDAXPublicOrPrivate = Union[gdax.AuthenticatedClient, gdax.PublicClient]


class Exchange:
    def __init__(self, config):
        self.type = config.get('type')
        if self.type not in ('gdax', 'bitfinex'):
            raise ValueError('exchange type is invalid: %s', self.type)

        exchanges = {
            'gdax': {
                'auth': gdax.AuthenticatedClient,
                'public': gdax.PublicClient,
                'maker_fee': D(0),
                'taker_fee': D(0.003),
            },
            'bitfinex': {
                'auth': bitfinex.GDAXInterfaceAdapter,
            }
        }
        key = config.get('key')
        secret = config.get('secret')
        passphrase = config.get('passphrase')

        if key is None or secret is None:
            raise ValueError('Please set both "key" and "secret" attributes')
        if self.type == 'gdax' and passphrase is None:
            raise ValueError('GDAX exchange requires the "passphrase" attribute')

        self.taker_fee = exchanges[self.type].get('taker_fee', D(0))
        self.maker_fee = exchanges[self.type].get('marker_fee', D(0))
        self.auth = exchanges[self.type]['auth'](*config[1:])

        public = exchanges[self.type].get('public')
        if public is not None:
            self.public = public()
        else:
            self.public: GDAXPublicOrPrivate = self.auth

        self.balance = {}

    def refresh_balance(self):
        reply = self.auth.get_accounts()
        for currency in reply:
            name = currency['currency']
            self.balance[name] = D(currency['available'])
        return self.balance


class AccountState:
    def __init__(self, name: str, initial_balance: Dict[str, StrOrNone],
                 virtual: bool = True):
        self.name = name
        self.virtual = virtual
        self.balance = {}
        for cur, amount in initial_balance.items():
            self.balance[cur] = D(amount) if amount is not None else None
        self.pairs: Dict[str, Pair] = {}
        self.pending_orders = Set[str] = set()


class Pair:
    def __init__(self, _id: str):
        self.id = _id
        self.pending_orders: List[OrderState] = []
        self.size = D(0)
        self.filled_size = D(0)
        self.executed_value = D(0)
        self.rule: Rule = None
        self.tweet: Tweet = None
        self.status: str = None
        self.position = None
        self.expiration: int = 0

    def update(self, rule: Rule, tweet: Tweet):
        if self.tweet is not None and self.tweet.id >= tweet.id:
            log.info("Ignoring tweet with id: %s because because current id "
                     "is newer: %s Tweet: %s, Tweet date: %s",
                     tweet.id, self.tweet.id, tweet.text, tweet.created)
            return

        log.info("Updating pair %s based on new tweet info: %s",
                 self.id, tweet.text)

        self.tweet = tweet
        self.rule = rule
        self.expiration = tweet.created_ts + rule.ttl
        self.size = D(0)
        self.filled_size = D(0)
        self.executed_value = D(0)
        self.status = None

        if int(time.time()) > self.expiration:
            log.warning("Got new advice from an expired tweet. Ignoring... "
                        "Tweet date: %s, order: %s", tweet.created,
                        rule.order_template)
            self.status = 'expired'
            return

        log.info("Updating pair %s with tweet id: %s, tweet text: %s",
                 self.id, tweet.id, tweet.text)


class Account:
    def __init__(self, exchange: Exchange, rules: List[Rule], state: AccountState):
        self.exchange = exchange
        self.rules = rules
        self.name = state.name
        self.pairs: Dict[str, Pair] = state.pairs
        self.balance: Dict[str, Decimal] = state.balance
        self.pending_orders: Set[str] = state.pending_orders
        self.virtual = state.virtual
        self.lock = threading.Lock()

    def refresh_balance(self, status_update=False, status_csv=False):
        """ Retrieve balance in exchange account

            status_update: True iff status update should be printed
            status_csv: True iff a csv-formatted line should be printed

            Return value: dictionary mapping currency to account information
        """
        balance = self.get_accounts()
        if status_update:
            balance_str = ', '.join('%s: %s' % (p, round_down(a))
                                    for p, a in balance.items())
            log.info('Current balance in wallet: %s' % balance_str)
        if status_csv:
            now = datetime.datetime.now()
            # TODO - do this log for the pairs we are trading (retrieved from config)
            balance_csv = (
                "%s, balance, EUR-ETH-BTC, %s, %s, %s, bids, "
                "BTC-EUR ETH-EUR ETH-BTC, %s, %s, %s"
            )
            balance_csv = balance_csv % (
                now.strftime("%Y-%m-%d %H:%M:%S"),
                round_down(balance['EUR']),
                round_down(balance['ETH']),
                round_down(balance['BTC']),
                get_price(self.exchange.auth, 'BTC-EUR'),
                get_price(self.exchange.auth, 'ETH-EUR'),
                get_price(self.exchange.auth, 'ETH-BTC')
            )
            log.info('csv %s' % balance_csv)

        return balance

    def get_product_order_book(self, *args, **kwargs) -> Dict[str, List[List[str]]]:
        return self.exchange.public.get_product_order_book(*args, **kwargs)

    def get_accounts(self) -> Dict[str, Decimal]:
        currencies = self.exchange.auth.get_accounts()
        for name, exchange_amount in currencies.items():
            amount = self.balance[name]
            if amount is None or not self.virtual:
                amount = D(exchange_amount)
            self.balance[name] = min(amount, exchange_amount)
        return self.balance

    def _update_balance_from_order(self, order_state: OrderState):
        if not self.virtual:
            return
        if order_state.status != 'done':
            return
        if order_state.id in self.pending_orders:
            self._update_balance_from_pending_order(order_state)

    def _wait_for_order(self, _id: str, ttl: int) -> Union[OrderState, None]:
        order_state = None
        for i in range(ttl):
            order_state = None
            try:
                order_state = self.get_order(_id)
            except OrderNotFound:
                log.error("Server said immediate order %s is not found. "
                          "This should have not happened.", _id)
                break
            except OrderError as oerr:
                log.error("Unspecified error while trying to fetch order "
                          "%s: %s", _id, oerr)
            else:
                if order_state.status == 'done':
                    log.info("Order %s done after %d seconds.", _id, i)
                    break

            time.sleep(1)

        if order_state is None or order_state.status != 'done':
            log.error("Tried to update balance, but order %s is not yet done.",
                      _id)

        return order_state

    def _update_balance_from_immediate_order(self, order_state: OrderState):
        if not self.virtual:
            return

        if order_state.status != 'done':
            # An immediate order is expected to finish quickly. Wait a few
            # seconds until it does.
            order_state = self._wait_for_order(order_state.id, ttl=30)
            if order_state is None:
                return

        base_currency, quote_currency = order_state.product_id.split('-', 1)
        if order_state.side == 'buy':
            self.balance[base_currency] += order_state.filled_size
            self.balance[quote_currency] -= order_state.executed_value + order_state.fill_fees
        elif order_state.side == 'sell':
            self.balance[base_currency] -= order_state.filled_size
            self.balance[quote_currency] += order_state.executed_value
        else:
            log.error("Could not determine order side: %s",
                      order_to_dict(order_state))

    def _update_balance_from_pending_order(self, order_state: OrderState):
        if not self.virtual:
            return
        if order_state.id not in self.pending_orders:
            log.error('Tried to update balance with an order that does not '
                      'appear to be a pending order: %s',
                      order_to_dict(order_state))
            return
        if order_state.status != 'done':
            log.error("Tried to update balance with an order that has not yet "
                      "finished: %s", order_to_dict(order_state))
            return

        base_currency, quote_currency = order_state.product_id.split('-', 1)
        refund = D(0)
        refund_currency = None
        if order_state.side == 'buy':
            self.balance[base_currency] += order_state.filled_size
            refund = order_state.size * order_state.price
            if not order_state.post_only:
                refund += refund * self.exchange.taker_fee
            refund -= order_state.executed_value - order_state.fill_fees
            refund_currency = quote_currency
        elif order_state.side == 'sell':
            self.balance[quote_currency] += order_state.executed_value
            refund = order_state.size - order_state.filled_size
            refund_currency = base_currency
        else:
            log.error("Could not determine order side: %s",
                      order_to_dict(order_state))

        if refund < D(0):
            log.error('Refund < 0! Please investigate this: %s',
                      order_to_dict(order_state))
            refund = D(0)

        self.balance[refund_currency] += refund

        self.pending_orders.remove(order_state.id)

    def get_order(self, order_id: str) -> Union[OrderState, None]:
        r = self.exchange.auth.get_order(order_id)
        self._handle_errors(r)
        order_state = OrderState(r)
        self._update_balance_from_order(order_state)

        return r

    def cancel_order(self, order_id):
        r = self.exchange.auth.cancel_order(order_id)
        self._handle_errors(r)
        if order_id in self.pending_orders:
            time.sleep(0.5)
            order_state = self.get_order(order_id)
            self._update_balance_from_pending_order(order_state)
        return r

    def _handle_errors(self, reply: Dict[str, str]):
        if 'message' not in reply:
            return

        msg = reply['message'].lower()
        if 'insufficient funds' in msg:
            raise InsufficientFunds(msg)
        elif 'order size is too small' in msg:
            raise OrderSizeTooSmall(msg)
        elif 'not found' in msg:
            raise OrderNotFound(msg)
        else:
            raise OrderError(msg)

    def _capture_balance_for_limit_order(self, order: Order):
        if not self.virtual:
            return None, None
        base_currency, quote_currency = order.product_id.split('-', 1)

        if order.type == 'market' or order.time_in_force == 'FOK':
            return None, None

        if order.side == 'buy':
            capture = order.size * order.price
            if not order.post_only:
                capture += capture * self.exchange.taker_fee
            if capture > self.balance[quote_currency]:
                raise InsufficientFunds()
            captured = (quote_currency, capture)
            self.balance[quote_currency] -= capture
        elif order.side == 'sell':
            capture = order.size
            if capture > self.balance[base_currency]:
                raise InsufficientFunds()
            captured = (base_currency, capture)
            self.balance[base_currency] -= capture
        else:
            raise ValueError('Order side is invalid: %s', order_to_dict(order))

        return captured

    def _order_action(self, order: Order, action: Callable) -> OrderState:
        order_dict = order_to_dict(order, strict=True)
        if order.type == 'market' and order.price is not None:
            del order_dict['price']

        currency = None
        captured_amount = None
        order_state = None
        try:
            currency, captured_amount = self._capture_balance_for_limit_order(order)
            r = action(**order_dict)
            order.raw_server_reply = r
            self._handle_errors(r)
            order_state = OrderState(r)
        finally:
            # Pending order (limit GTC, limit GTT, ...).
            if captured_amount is not None:
                # Succeeded: add to pending orders set.
                if order_state is not None:
                    self.pending_orders.add(order_state.id)
                # Failed: refund.
                else:
                    self.balance[currency] += captured_amount

            # Immediate order (market, fill-or-kill, ...).
            else:
                # Succeeded: update funds.
                if order_state is not None:
                    self._update_balance_from_immediate_order(order_state)

        return order_state

    def buy(self, order: Order) -> OrderState:
        return self._order_action(order, self.exchange.auth.buy)

    def sell(self, order: Order) -> OrderState:
        return self._order_action(order, self.exchange.auth.sell)


def get_price(gdax, pair):
    """ Retrieve bid price for a pair

        gdax: any object implementing the GDAX API
        pair: The pair that we want to know the price
        Return value: string with the pair bid price
    """
    try:
        order_book = gdax.get_product_order_book(pair)
    except KeyError:
        return 'NA'

    return D(order_book['bids'][0][0])
