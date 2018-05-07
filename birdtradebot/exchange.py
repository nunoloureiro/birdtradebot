import logging
import threading
import time
import uuid
from copy import deepcopy
from decimal import Decimal
from typing import Dict, List, Union, Callable

import gdax

from exchanges import bitfinex
from order import (
    Order,
    OrderState,
    ActiveOrder,
    OrderError,
    InsufficientFunds,
    OrderSizeTooSmall,
    OrderNotFound,
    order_to_dict)
from rule import Rule
from twitter import Tweet
from utils import round_down, D

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

        if not key or not secret:
            raise ValueError('Please set both "key" and "secret" attributes')
        if self.type == 'gdax' and not passphrase:
            raise ValueError('GDAX exchange requires the "passphrase" attribute')

        self.taker_fee = exchanges[self.type].get('taker_fee', D(0))
        self.maker_fee = exchanges[self.type].get('marker_fee', D(0))
        self.auth = exchanges[self.type]['auth'](key, secret, passphrase)

        public = exchanges[self.type].get('public')
        if public is not None:
            self.public = public()
        else:
            self.public: GDAXPublicOrPrivate = self.auth

        self.balance = {}
        self.lock = threading.Lock()

    def refresh_balance(self, extra_pairs=None):
        reply = self.auth.get_accounts()
        for currency in reply:
            name = currency['currency']
            self.balance[name] = D(currency['available'])

        # Make sure that the currency for every extra pair has a balance.
        # This balance will be used to determine the amount to buy or sell when
        # creating exchange orders.
        pairs = extra_pairs if extra_pairs else []
        for pair in pairs:
            for symbol in pair.split('-'):
                if symbol not in self.balance:
                    self.balance[symbol] = D('0')

        return self.balance

    def create_client_oid(self):
        return None
        #if self.type == 'gdax':
        #    return None  # str(uuid.uuid4())
        #elif self.type == 'bitfinex':
        #    return str(uuid.uuid1().int >> 64)
        #else:
        #    return None


class AccountState:
    def __init__(self, name: str, initial_balance: Dict[str, StrOrNone],
                 virtual: bool = False):
        self.name = name
        self.virtual = virtual
        self.balance = {}
        if initial_balance is None:
            initial_balance = {}
        for cur, amount in initial_balance.items():
            self.balance[cur] = D(amount) if amount is not None else None
        self.pairs: Dict[str, Pair] = {}
        self.pending_orders: Dict[str, ActiveOrder] = {}
        self.unconfirmed_orders: List[ActiveOrder] = []
        self.done_orders: Dict[str, OrderState] = {}


class Pair:
    def __init__(self, product_id: str):
        self.product_id = product_id
        self.pending_orders: List[OrderState] = []
        self.size = D(0)
        self.filled_size = D(0)
        self.executed_value = D(0)
        self.rule: Rule = None
        self.status: str = 'done'
        self.settled: bool = False
        self.position = None
        self.expiration: int = 0
        self.twitter: Dict[str, Tweet] = {}
        self.updated = False
        self.previous: Pair = None
        self.base_currency, self.quote_currency = product_id.split('-', 1)
        self.errors = 0

    def update_balance(self, order_state: OrderState):
        log.debug("Updating pair %s balance from order %s: %s",
                  self.product_id, order_state.id, order_to_dict(order_state))
        self.filled_size += order_state.filled_size
        self.executed_value += order_state.executed_value

    def update(self, rule: Rule, tweet: Tweet):
        our_tweet = self.twitter.get(tweet.handle)
        if our_tweet is not None and tweet.id <= our_tweet.id:
            log.info("Saved tweet id is more recent than new tweet id (%s >= %s). "
                     "Handle: %s. Text: %s. Date: %s",
                     our_tweet.id, tweet.id, tweet.handle, tweet.text,
                     tweet.created)
            return

        tweet.position = 'long' if rule.order_template.side == 'buy' else 'short'
        self.twitter[tweet.handle] = tweet

        if rule.agreement_handles:
            have_agreement = True
            for h in rule.agreement_handles:
                try:
                    saved_tweet = self.twitter[h]
                except KeyError:
                    pass
                else:
                    have_agreement &= tweet.position == saved_tweet.position
            if not have_agreement:
                log.warning("Ignoring tweet because an agreement could "
                            "not be reached.")
                return

        log.info("Updating pair %s based on new tweet info: %s, position: %s",
                 self.product_id, tweet.text, tweet.position)

        now = int(time.time())
        if now > tweet.created_ts + rule.tweet_ttl:
            log.warning("Advice is from an expired tweet. Ignoring... "
                        "Tweet date: %s, Tweet handle: %s, order: %s",
                        tweet.created, tweet.handle,
                        order_to_dict(rule.order_template))
            return

        log.info("Updating pair %s with tweet id: %s, tweet text: %s",
                 self.product_id, tweet.id, tweet.text)
        self.updated = True
        self.errors = 0
        self.previous = None
        self.previous = deepcopy(self)
        self.rule = rule
        self.expiration = now + rule.ttl
        self.size = D(0)
        self.filled_size = D(0)
        self.executed_value = D(0)
        self.status = None
        self.position = None
        self.settled = False


class Account:
    def __init__(self, exchange: Exchange, rules: List[Rule],
                 state: AccountState, save_state: Callable):
        self.exchange = exchange
        self.rules = rules
        self.name = state.name
        self.pairs: Dict[str, Pair] = state.pairs
        self.balance: Dict[str, Decimal] = state.balance
        self.unconfirmed_orders: List[ActiveOrder] = state.unconfirmed_orders
        self.pending_orders: Dict[str, ActiveOrder] = state.pending_orders
        self.done_orders: Dict[str, OrderState] = state.done_orders
        self.virtual = state.virtual
        self.save_state = save_state
        self.order_ttl = 3600

    def forget_old_orders(self):
        now = int(time.time())
        for order in list(self.done_orders.values()):
            if now > order.timestamp + self.order_ttl:
                del self.done_orders[order.id]

    def cancel_pending_orders(self):
        for order_id in list(self.pending_orders.keys()):
            self.cancel_order(order_id)
        self.pending_orders.clear()

    def refresh_balance(self, status_update=False):
        """
            Retrieve balance in exchange account

            status_update: True iff status update should be printed
            status_csv: True iff a csv-formatted line should be printed

            Return value: dictionary mapping currency to account information
        """
        balance = self.get_accounts()
        exchange_balance = self.exchange.balance
        if status_update:
            balance_str = ', '.join('%s: %s (total: %s)' % (
                p, round_down(a), round_down(exchange_balance[p], 2))
                for p, a in balance.items())
            log.info('Current balance in %s: %s' % (self.name, balance_str))

        return balance

    def get_product_order_book(self, *args, **kwargs) -> Dict[str, List[List[str]]]:
        return self.exchange.public.get_product_order_book(*args, **kwargs)

    def get_accounts(self) -> Dict[str, Decimal]:
        exchange_balance = self.exchange.refresh_balance(
                extra_pairs=self.pairs.keys())
        for symbol, exchange_amount in exchange_balance.items():
            if symbol not in self.balance:
                self.balance[symbol] = D('0')
            amount = self.balance[symbol]
            if amount is None or not self.virtual:
                amount = exchange_amount
            self.balance[symbol] = min(amount, exchange_amount)

        return self.balance

    def get_open_orders(self, since=None, product_id=None):
        if self.exchange.type != 'bitfinex':
            return []
        return [
            OrderState(o)
            for o in self.exchange.auth.get_open_orders(since, product_id)
        ]

    def get_closed_orders(self, since=None, product_id=None):
        if self.exchange.type != 'bitfinex':
            return []
        return [
            OrderState(o)
            for o in self.exchange.auth.get_closed_orders(since, product_id)
        ]

    def wait_for_order(self, _id: str, ttl: int=60) -> Union[OrderState, None]:
        log.info("Waiting for order %s to complete...", _id)
        order_state = None
        start_ts = time.time()
        elapsed = 0
        while elapsed < ttl:
            order_state = None
            try:
                order_state = self.get_order(_id, ttl=ttl)
            except OrderNotFound as onf:
                log.error("Server said order %s was not found: %s "
                          "This should have not happened.", _id, onf)
                break
            except OrderError as oerr:
                log.error("Unspecified error while trying to fetch order "
                          "%s: %s", _id, oerr)
            else:
                if order_state is None:
                    log.error("Could not get order state for order %s!", _id)
                    break

                if order_state.status == 'done':
                    log.info("Order %s done after %s seconds.", _id, elapsed)
                    break
                else:
                    log.info("Order %s is not yet done. %s seconds elapsed",
                             _id, elapsed)

            time.sleep(2)
            elapsed = time.time() - start_ts

        return order_state

    def _update_balance_from_order(self, order_state: OrderState):
        if (order_state.id not in self.pending_orders or
                order_state.status != 'done'):
            return

        pending = self.pending_orders.pop(order_state.id)
        self.done_orders[order_state.id] = order_state
        if not self.virtual:
            return

        log.info("Updating virtual balance from order %s. " 
                 "Waiting a few seconds to ensure that balance is refreshed...",
                 order_state.id)
        time.sleep(10)

        bc, qc = order_state.product_id.split('-', 1)
        prev_bc, prev_qc = self.balance[bc], self.balance[qc]

        if order_state.side == 'buy':
            refund = pending.captured - order_state.executed_value - order_state.fill_fees
            self.balance[bc] += order_state.filled_size
            self.balance[qc] += refund
        else:
            self.balance[bc] += order_state.size - order_state.filled_size
            self.balance[qc] += order_state.executed_value - order_state.fill_fees

        bc_delta = self.balance[bc] - prev_bc
        qc_delta = self.balance[qc] - prev_qc
        bc_sign = '+' if bc_delta >= 0 else '-'
        qc_sign = '+' if qc_delta >= 0 else '-'

        log.info("Updated virtual balance for %s. %s: %s%s, %s: %s%s. "
                 "Order details: %s",
                 self.name,
                 bc, bc_sign, bc_delta,
                 qc, qc_sign, qc_delta,
                 order_to_dict(order_state))

        self.balance[bc] = max(self.balance[bc], D(0))
        self.balance[qc] = max(self.balance[qc], D(0))

    def get_order(self, order_id: str, ttl=60) -> Union[OrderState, None]:
        log.info("Getting order %s...", order_id)
        elapsed = 0
        start_ts = time.time()
        order_state = None
        attempt = 0
        while elapsed < ttl:
            log.info("Getting order %s (attempt %s)...", order_id, attempt)
            attempt += 1
            r = self.exchange.auth.get_order(order_id)
            log.debug("Get order %s reply: %s", order_id, r)
            try:
                self._handle_errors(r)
            except OrderNotFound:
                log.info("Trying to get order %s, but server could "
                         "not find it. Will keep retrying for %s seconds...",
                         order_id, ttl - elapsed)
            else:
                order_state = OrderState(r)
                self._update_balance_from_order(order_state)
                break

            time.sleep(2)
            elapsed = time.time() - start_ts

        return order_state

    def cancel_order(self, order_id):
        log.debug("Cancelling order %s...", order_id)
        ttl = 60
        elapsed = 0
        start_ts = time.time()
        order_state = None
        while elapsed < ttl:
            r = self.exchange.auth.cancel_order(order_id)
            log.debug("Cancel order %s reply: %s", order_id, r)
            try:
                self._handle_errors(r)
            except OrderNotFound:
                log.info("Trying to cancel pending order %s, but server could "
                         "not find it. Will keep retrying for %s seconds...",
                         order_id, ttl - elapsed)
            else:
                if order_id in self.pending_orders:
                    order_state = self.wait_for_order(order_id, ttl=30)
                    if order_state is not None:
                        self._update_balance_from_order(order_state)
                break

            time.sleep(2)
            elapsed = time.time() - start_ts

        return order_state

    def _handle_errors(self, reply: Dict[str, str]):
        if reply is None:
            raise OrderError('Unknown order error')

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

    def _capture_balance_for_order(self, order: Order):
        if not self.virtual:
            return None, None

        base_currency, quote_currency = order.product_id.split('-', 1)
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

        log.info('Captured %s %s on account %s',
                 captured[1], captured[0], self.name)

        return captured

    def _order_action(self, order: Order, action: Callable) -> OrderState:
        order.client_oid = self.exchange.create_client_oid()
        order_dict = order_to_dict(order, strict=True)
        if order.type == 'market' and order.price is not None:
            del order_dict['price']

        order_state = None
        try:
            currency, captured_amount = self._capture_balance_for_order(order)
            active = ActiveOrder(order, currency, captured_amount)
            if len(self.unconfirmed_orders) > 0:
                log.error("Unconfirmed orders list is not empty!: %s",
                          self.unconfirmed_orders)
                del self.unconfirmed_orders[:]
            self.unconfirmed_orders.append(active)
            log.debug("Added %s order to unconfirmed orders list: %s",
                      order.type, order_to_dict(order))
            self.save_state()
            r = action(**order_dict)
            order.raw_server_reply = r
            self._handle_errors(r)
            order_state = OrderState(r)
        finally:
            if self.unconfirmed_orders:
                active = self.unconfirmed_orders.pop()
                log.debug("Cleared unconfirmed orders list...")
                if order_state is not None:
                    active.order_state = order_state
                    self.pending_orders[order_state.id] = active
                else:
                    log.warning("Order was not placed. "
                                "Returning %s %s to account %s",
                                active.captured, active.currency, self.name)
                    self.balance[active.currency] += active.captured
            self.save_state()

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
