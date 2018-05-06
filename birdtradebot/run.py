#!/usr/bin/env python
"""
birdtradebot

Checks tweets and uses config specified in file to make market trades on
exchanges using a GDAX-like API. Configuration is stored in config/config.py
and follows the tweets of @birdpersonborg.
"""

import argparse
import decimal
import queue
import os
import random
import sys
import time
import threading
import copy

# In case user wants to use regular expressions or math on conditions/funds
import math
import re

from decimal import Decimal
from typing import List, Dict, Set, Union, Callable

decimal.getcontext().rounding = decimal.ROUND_DOWN

_help_intro = """birdtradebot allows users to base trades on tweets."""

import twython

import logging

logging.basicConfig(
    format='%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG)
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

loggers = [
        'requests', 'urllib3', 'requests_oauthlib', 'oauthlib',
        'ccxt'
]

for l in loggers:
    logging.getLogger(l).setLevel(logging.WARNING)

from utils import round_down, D, split_amount
from exchange import (
    Account,
    AccountState,
    Pair,
    Exchange)
from rule import Rule
from twitter import Tweet, TwitterState
from app_state import (
    load_app_state,
    save_app_state,
    AppState)
from order import (
    Order,
    OrderState,
    OrderTemplate,
    OrderBatch,
    OrderError,
    OrderSizeTooSmall,
    OrderNotFound,
    OrderExpired,
    InsufficientFunds,
    order_to_dict,
    orders_match
)

PRICE_PRECISION = {
    'ETH-EUR': 2,
    'BTC-EUR': 2,
    'ETH-BTC': 5,
    'IOT-USD': 4,
    'EOS-ETH': 6,
}


def help_formatter(prog):
    """ So formatter_class's max_help_position can be changed. """
    return argparse.HelpFormatter(prog, max_help_position=40)


def relevant_tweet(tweet, rule: Rule, balance: Dict[str, Decimal]):
    if (
            # Check if this is a user we are following
            (not rule.handles or tweet.handle and tweet.handle.lower() in rule.handles)

            and

            # Check if the tweet text matches any defined condition
            (not rule.keywords or any([keyword in tweet.text.lower() for keyword in rule.keywords]))

            and

            eval(rule.condition.format(tweet='tweet.text', available=balance))):

        # Check if this is an RT or reply
        if (tweet.retweeted_status or tweet.in_reply_to_status_id or
                tweet.in_reply_to_status_id_str or tweet.in_reply_to_user_id or
                tweet.in_reply_to_user_id_str or tweet.in_reply_to_screen_name):
            return False

        return True

    return False


def twitter_handles_to_userids(twitter, handles):
    ids_map = {}

    for handle in handles:
        try:
            ids_map[handle] = twitter.show_user(screen_name=handle)['id_str']
        except twython.TwythonError as e:
            msg = getattr(e, 'message', None)
            if msg is not None and 'User not found' in msg:
                log.warning('Handle %s not found; skipping rule...' % handle)
            else:
                raise

    if not ids_map:
        raise RuntimeError('No followable Twitter handles found in config!')

    return ids_map


def calc_buy_size(account: Account, pair: Pair,
                  ask: Decimal, bid: Decimal, price: Decimal) -> Decimal:
    quote_currency = pair.quote_currency
    short_pairs = []
    for pair in account.pairs.values():
        # Only count pairs with the same quote currency, e.g., *-EUR, *-BTC
        if not pair.product_id.endswith('-%s' % quote_currency):
            continue

        if pair.position == 'long':
            continue

        # TODO: check if this is right. It depends on whether the exchange
        # updates the balance as orders are being placed or not
        if pair.position == 'short':
            short_pairs.append(pair.product_id)
            continue

        # No position (short or long) yet. (i.e., no order has completed).
        buying = pair.rule.order_template.side == 'buy'
        selling = pair.rule.order_template.side == 'sell'

        if pair.status == 'expired':
            # The twitter bot went short.
            if selling: short_pairs.append(pair.product_id)
            # The twitter bot went long.
            if buying: pass

        # The order has not expired. So we are still changing our state.
        else:
            # Buying. Ensure we get our share of the available balance.
            if buying: short_pairs.append(pair.product_id)
            # Selling.
            if selling: pass

    log.debug("The following pairs are short: %s", short_pairs)
    n_short_pairs = len(short_pairs)
    if pair.rule.order_template.size == '{split_size}':
        size = account.balance[quote_currency] / D(n_short_pairs) / price
    else:
        max_account_buy_size = account.balance[quote_currency] / price

        if isinstance(pair.rule.order_template.size, Decimal):
            size = pair.rule.order_template.size
        else:
            size = eval(pair.rule.order_template.size.format(
                inside_ask=ask,
                inside_bid=bid,
                balance=account.balance,
                max_account_buy_size=max_account_buy_size,
            ))

    return D(round_down(size))


def calc_sell_size(account: Account, pair: Pair, ask, bid):
    base_currency = pair.base_currency
    max_account_sell_size = account.balance[base_currency]

    if isinstance(pair.rule.order_template.size, Decimal):
        size = pair.rule.order_template.size
    else:
        size = eval(pair.rule.order_template.size.format(
            inside_ask=ask,
            inside_bid=bid,
            balance=account.balance,
            max_account_sell_size=max_account_sell_size,
        ))

    return D(round_down(size))


def new_order(account: Account, pair: Pair,
              template: OrderTemplate=None) -> Order:
    # Update available funds before calculating order size.
    account.refresh_balance(status_update=True)
    if template is None:
        template = pair.rule.order_template

    order_book = account.get_product_order_book(template.product_id)
    inside_bid = D(order_book['bids'][0][0])
    inside_ask = D(order_book['asks'][0][0])

    precision = PRICE_PRECISION.get(template.product_id, 2)
    price = D(eval(template.price.format(
        inside_bid=inside_bid,
        inside_ask=inside_ask)))

    order_dict = order_to_dict(template, strict=True)
    order_dict['price'] = str(round_down(price, precision))

    if template.side == 'buy':
        size = calc_buy_size(account, pair, inside_ask, inside_bid, price)
    elif template.side == 'sell':
        size = calc_sell_size(account, pair, inside_ask, inside_bid)
    else:
        raise ValueError("Unrecognized order side: %s", template.side)

    order_dict['size'] = str(size)
    log.info("%s order built: %s", order_dict['type'], order_dict)

    return Order(order_dict)


def place_orders(action: Callable, orders: OrderBatch) -> OrderBatch:
    new_orders = list(orders.new)
    del orders.new[:]

    for order in new_orders:
        try:
            order_state: OrderState = action(order)
        except InsufficientFunds:
            log.error('Could not place %s order due to insufficient funds: %s',
                      order.type, order_to_dict(order))
            order.error = InsufficientFunds
            orders.error.append(order)
        except OrderSizeTooSmall:
            log.error('Could not place %s order because size is too small: %s',
                      order.type, order_to_dict(order))
            order.error = OrderSizeTooSmall
            orders.error.append(order)
        except OrderError as oe:
            log.error("%s order error %s: %s", order.type, oe, order_to_dict(order))
            order.error = OrderError
            orders.error.append(order)
        else:
            log.info("New order successfully created with id %s", order_state.id)
            if order_state.status == 'done':
                orders.done.append(order_state)
            else:
                orders.pending.append(order_state)

        if len(new_orders) > 1:
            time.sleep(round_down(2 + random.random(), 2))

    return orders


def check_pending_orders(account: Account, orders: OrderBatch) -> OrderBatch:
    pending = list(orders.pending)
    del orders.pending[:]

    for order in pending:
        try:
            state = account.get_order(order.id)
        except OrderNotFound as onf:
            order.error = OrderNotFound
            log.warning("Order %s not found: %s", order.id, onf)
            orders.error.append(order)
        else:
            if state.status == 'done':
                orders.done.append(state)
            else:
                orders.pending.append(state)

        time.sleep(0.5)

    return orders


def split_order(main_order: Order, max_size: Decimal, truncate=5) -> List[Order]:
    if main_order.size > max_size:
        order_sizes = split_amount(main_order.size, max_size * 0.8, max_size)
    else:
        order_sizes = [main_order.size]

    orders = []
    for size in order_sizes[:truncate]:
        order_dict = order_to_dict(main_order, strict=True)
        order_dict['size'] = str(size)
        orders.append(Order(order_dict))

    return orders


def check_expired_orders(orders: OrderBatch, ttl: int) -> OrderBatch:
    now = int(time.time())
    pending = list(orders.pending)
    del orders.pending[:]

    for order in pending:
        if order.timestamp + ttl > now:
            log.debug("Order %s is still valid. expiry: %d > now: %s",
                      order.id, order.timestamp + ttl, now)
            orders.pending.append(order)
            continue

        order.error = OrderExpired
        orders.error.append(order)

    return orders


def split_and_place_limit_order(account: Account, pair: Pair,
                                orders: OrderBatch) -> OrderBatch:

    main_order = new_order(account, pair)
    if main_order.size == D(0):
        log.warning("Will not place limit order with size 0: %s",
                    order_to_dict(main_order))
        return

    if pair.size == D(0):
        pair.size = main_order.size

    split_order_size = pair.rule.split_order_size
    if main_order.type == 'limit' and split_order_size > D(0):
        max_order_size = split_order_size
    else:
        max_order_size = main_order.size

    action = account.buy if main_order.side == 'buy' else account.sell

    max_pending_orders = 10
    if len(orders.pending) >= max_pending_orders:
        return orders

    n_orders = max_pending_orders - len(orders.pending)
    orders.new = split_order(main_order, max_order_size, truncate=n_orders)
    orders = place_orders(action, orders)

    return orders


def check_error_orders(account: Account, pair: Pair, orders: OrderBatch):
    err = None
    for order in orders.error:
        if order.error == InsufficientFunds:
            log.warning("Insufficient funds to place order. %s",
                        order_to_dict(order))
            err = InsufficientFunds
        elif order.error == OrderSizeTooSmall:
            log.warning("Order size is too small: %s. %s",
                        order.size, order_to_dict(order))
            err = OrderSizeTooSmall
        else:
            log.warning("Will try to cancel order: %s", order_to_dict(order))
            try:
                order_id = order.id
            except AttributeError:
                pass
            else:
                account.cancel_order(order_id)
                order_state = account.wait_for_order(order_id)
                if order_state is not None:
                    pair.update_balance(order_state)

    del orders.error[:]

    return err


def check_done_orders(pair: Pair, orders: OrderBatch):
    too_many_errors = False
    for order in orders.done:
        log.info("Order is done: %s", order_to_dict(order))
        pair.update_balance(order)
        if not order.settled:
            log.warning("Order %s is done but not settled.", order.id)
            pair.errors += 1
            if pair.errors > 3:
                log.warning("Too many errors for pair %s. Bailing.",
                            pair.product_id)
                too_many_errors = True

    del orders.done[:]

    return too_many_errors


def place_limit_order(account: Account, pair: Pair):
    orders = OrderBatch()
    orders.pending = pair.pending_orders
    orders = check_pending_orders(account, orders)
    orders = check_expired_orders(orders, pair.rule.order_ttl)
    too_many_errors = check_done_orders(pair, orders)
    check_error_orders(account, pair, orders)

    if pair.size != 0 and pair.filled_size >= pair.size:
        pair.status = 'done'
        pair.settled = True
    elif too_many_errors:
        pair.status = 'done'
        pair.settled = False

    if pair.status == 'done':
        return

    if orders.pending:
        log.info("Pair %s still has pending non-expired orders. Waiting...",
                  pair.product_id)
        return

    # New orders
    orders = split_and_place_limit_order(account, pair, orders)
    if orders is None:
        pair.status = 'done'
        pair.settled = False
        return

    err = check_error_orders(account, pair, orders)
    if err in (InsufficientFunds, OrderSizeTooSmall):
        pair.status = 'done'
        pair.settled = False
        return


def place_market_order(account: Account, pair: Pair):
    order = copy.deepcopy(pair.rule.order_template)
    order.type = 'market'
    order.post_only = None
    order.time_in_force = None

    order = new_order(account, pair, template=order)
    orders = OrderBatch()
    orders.new.append(order)
    action = account.buy if order.type == 'buy' else account.sell
    orders = place_orders(action, orders)

    if orders.pending:
        order_state = orders.pending[0]
        order_state = account.wait_for_order(order_state.id)
        if order_state is not None:
            pair.settled = order_state.settled
            if order_state.status != 'done':
                log.error("Placed market order. It should not have been pending: %s",
                          order_to_dict(order_state))

    if orders.done and orders.done[0].settled:
        pair.update_balance(orders.done[0])
        pair.settled = True

    if orders.error and orders.error[0].error == InsufficientFunds:
        try:
            order_state = handle_insufficient_funds(account, order)
        except OrderError:
            log.warning("market order error: %s", order_to_dict(order))
        else:
            order_state = account.wait_for_order(order_state.id)
            if order_state is not None:
                pair.settled = order_state.settled
                if order_state.settled:
                    pair.update_balance(order_state)

    pair.status = 'done'


def update_account_position(account: Account, pairs: Set[str],
                            new_pairs: Set[str]):

    for product_id in list(new_pairs):
        if product_id in pairs:
            for order in account.pairs[product_id].pending_orders:
                try:
                    account.cancel_order(order.id)
                except OrderNotFound:
                    pass

        new_pairs.remove(product_id)
        pairs.add(product_id)

    for product_id in list(pairs):
        pair = account.pairs[product_id]
        update_pair_position(account, pair)
        if pair.status == 'done':
            for order in pair.pending_orders:
                account.cancel_order(order.id)
                order_state = account.wait_for_order(order.id)
                if order_state is not None:
                    pair.update_balance(order_state)

            del pair.pending_orders[:]
            pairs.remove(product_id)


def update_accounts_positions(accounts: Dict[str, Account], q: queue.Queue):
    """
        This function runs in a separate thread. It receives requests in the
        queue from the main thread. It places orders on the exchange so that
        each "pending pair" transitions to the position indicated by a specific
        tweet.
    """
    pairs: Dict[str, Set[str]] = {}
    new_pairs: Dict[str, Set[str]] = {}

    log.info("Exchange thread starting...")
    while True:
        while True:
            block = not pairs and not new_pairs
            try:
                if block:
                    log.debug("Exchange thread blocked until orders arrive...")
                account_name, product_id = q.get(block)
            except queue.Empty:
                break
            else:
                if account_name not in new_pairs:
                    new_pairs[account_name] = set()
                new_pairs[account_name].add(product_id)

        for account in accounts.values():
            with account.exchange.lock:
                if account.name not in new_pairs:
                    new_pairs[account.name] = set()
                if account.name not in pairs:
                    pairs[account.name] = set()
                try:
                    update_account_position(account, pairs[account.name],
                                            new_pairs[account.name])
                finally:
                    account.save_state()

                del new_pairs[account.name]
                if not pairs[account.name]:
                    del pairs[account.name]

        time.sleep(20)


def update_pair_position(account: Account, pair: Pair):

    market_order = pair.rule.order_template.type == 'market'

    if pair.rule.order_template.type == 'limit':
        if pair.status != 'done':
            place_limit_order(account, pair)
        if pair.settled:
            return

        # If we reach here, then the limit order was _not_ settled.
        # Check if we should try using a market order as fallback.
        if pair.status == 'done' or int(time.time()) > pair.expiration:
            # Before placing a market order, ensure no pending orders exist
            for order in pair.pending_orders:
                account.cancel_order(order.id)
                order = account.wait_for_order(order.id)
                if order is not None:
                    if order.status == 'done':
                        pair.update_balance(order)
            del pair.pending_orders[:]

            if not pair.rule.market_fallback:
                return
 
            pair.rule.market_fallback = False
            market_order = True

    if market_order:
        place_market_order(account, pair)


def handle_insufficient_funds(account: Account, order: Order) \
        -> Union[OrderState, None]:
    orig_order_size = order.size
    quote_currency = order.product_id.split('-', 1)[1]
    r = None

    for i in range(5):
        previous_balance = account.balance[quote_currency]
        account.refresh_balance(status_update=True)
        current_balance = account.balance[quote_currency]
        log.warning("Fallback: server said we have insufficient funds. "
                    "Current balance: %s, previous balance: %s.",
                    current_balance, previous_balance)

        if previous_balance > current_balance:
            break

        log.info("Fallback: decreasing buy order size...")

        # Decrease the size by 0.1% in each attempt.
        size = orig_order_size * (D('0.999') - D(i) * D('0.001'))
        order.size = D(round_down(size))
        log.info("Fallback: order: %s", order_to_dict(order))

        try:
            r = account.buy(order)
        except InsufficientFunds:
            log.warning("Insufficient funds: %s", order.raw_server_reply)
        else:
            log.info('Fallback: server reply: %s', order.raw_server_reply)
            break

        time.sleep(1)

    log.info("Fallback: finished.")

    return r


class TradingStateMachine:
    """ Trades cryptocurrencies based on tweets. """

    def __init__(self, account: Account, state: AppState, q: queue.Queue):
        self.account = account
        self.state = state
        self.queue = q

    def step(self, tweets: List[Tweet], stepnum):
        # Update state
        for tweet in tweets:
            self._update_pairs(tweet)
 
        for pair in self.account.pairs.values():
            if pair.status != 'done' and (stepnum == 0 or pair.updated):
                pair.updated = False
                self.queue.put((self.account.name, pair.product_id))

        self.has_run = True

    def _update_pairs(self, tweet: Tweet):
        log.debug("Got the following tweet: %s" % tweet.text)

        for rule in self.account.rules:
            if not relevant_tweet(tweet, rule, self.account.balance):
                continue

            # Relevant tweet. Do something with it...
            log.info("Rule match || @ %s: %s" % (tweet.handle, tweet.text))

            pair = self.account.pairs.get(rule.id)
            if pair is None:
                pair = Pair(rule.order_template.product_id)
            pair.update(rule, tweet)
            self.account.pairs[pair.product_id] = pair


def get_tweets(twitter_client, handles, twitter_state: TwitterState) -> List[Tweet]:
    ids_map = twitter_handles_to_userids(twitter_client, handles)
    tweets = []

    # Refresh Twitter bot positions
    for handle, uid in ids_map.items():
        try:
            latest_id = twitter_state.handles[handle].id
        except KeyError:
            latest_id = None

        while True:
            raw_tweets = twitter_client.get_user_timeline(
                user_id=uid, exclude_replies=True, since_id=latest_id,
                count=100)
            time.sleep(1)
            if not raw_tweets:
                break
            for t in raw_tweets:
                tweet = Tweet(t)
                if latest_id is None or tweet.id > latest_id:
                    latest_id = tweet.id
                twitter_state.update(tweet)
                tweets.append(tweet)

    return tweets


def trade_loop(app_state: AppState, accounts: Dict[str, Account], twitter_client,
               twitter_handles):
    # Start thread to update pair positions on the different accounts.
    q = queue.Queue()
    t = threading.Thread(target=update_accounts_positions,
                         args=(accounts, q))
    t.name = "OrderDispatcher"
    t.daemon = True
    t.start()

    next_status_ts = 0
    stepnum = 0
    while True:
        sleep_seconds = 60 + random.random() * 10
        now = time.time()

        # Get tweets
        try:
            tweets = get_tweets(twitter_client, twitter_handles, app_state.twitter)
        except twython.TwythonError as te:
            log.warning("Error fetching Twitter status: %s. "
                        "Will retry in %d s...", te, sleep_seconds)
            tweets = []

        # Trade
        for account in accounts.values():
            with account.exchange.lock:
                account.forget_old_orders()
                t = TradingStateMachine(account, app_state, q)
                t.step(tweets, stepnum)

        if now > next_status_ts:
            for account in accounts.values():
                with account.exchange.lock:
                    account.refresh_balance(status_update=True)
            next_status_ts = now + 3600

        stepnum += 1
        time.sleep(sleep_seconds)


def trade(args):
    from config import config
    try:
        # Saved application state
        app_state = load_app_state(args.state)
        state_lock = threading.Lock()

        def save_state():
            with state_lock:
                save_app_state(args.state, app_state)

        # Twitter client, and handles and keywords to monitor
        twitter_handles, twitter_keywords = set(), set()
        twitter_client = twython.Twython(**config.twitter)

        # Exchanges
        exchanges = {}
        for name, creds in config.exchanges.items():
            exchanges[name] = Exchange(creds)

        # Accounts
        accounts: Dict[str, Account] = {}
        account_states = app_state.accounts
        for name, acct in config.accounts.items():
            rules = []
            for r in acct['rules']:
                rule = Rule(r)
                twitter_handles.update(rule.handles)
                twitter_keywords.update(rule.keywords)
                rules.append(rule)

            account_state = account_states.get(name)
            exchange = exchanges[acct['exchange']]
            if account_state is None:
                virtual = acct.get('virtual')
                account_state = AccountState(name, acct['initial_balance'],
                                             virtual)
                account_states[name] = account_state
            account = Account(exchange, rules, account_state, save_state)
            accounts[name] = account
            for active_order in account.pending_orders.values():
                order_state = active_order.order_state
                if order_state is None:
                    continue
                pair = account.pairs.get(order_state.product_id)
                if pair is None:
                    pair = Pair(order_state.product_id)
                found = False
                for o in pair.pending_orders:
                    if o.id == order_state.id:
                        found = True
                        break
                if not found:
                    pair.pending_orders.append(order_state)

    except Exception as exc:
        log.exception(exc)
        log.error('Chances are, this opaque error happened because you '
                  'entered incorrect security credentials '
                  'when you were configuring birdtradebot.\n')
        sys.exit(1)

    log.info('Twitter/Exchange credentials verified.')

    # Cancel and remove any pending order that may have lingered if the bot
    # was not properly shut down.
    known_order_ids = set()
    for account in accounts.values():
        with account.exchange.lock:
            known_order_ids.update(account.pending_orders.keys())
            known_order_ids.update(account.done_orders)
            account.save_state()

    # Try to find unconfirmed orders. Unconfirmed orders may have been created
    # on the server, before we could get a confirmation (order id).
    # There is no great way to find unconfirmed orders. In many cases, we cannot
    # use a local order id to verify if a given local order matches a server
    # order. However, we can look for it by listing recent server orders which
    # match a given criteria, such as product_id, side, size, type, among others.
    # If the server order id does not belong to a previous known order id, and
    # the criteria matches, then there is a good chance that this is the order
    # we are looking for.
    for account in accounts.values():
        if not account.unconfirmed_orders:
            continue
        lost_order = account.unconfirmed_orders.pop()
        with account.exchange.lock:
            order_found = False
            for order_lister in (account.get_open_orders,
                                 account.get_closed_orders):
                server_orders = order_lister(
                    since=lost_order.timestamp - account.order_ttl,
                    product_id=lost_order.order.product_id
                )
                for server_order in server_orders:
                    if server_order.id in known_order_ids:
                        continue
                    if not orders_match(server_order, lost_order.order):
                        continue

                    # This seems to be the right order. Cancel it.
                    # Add the lost order to pending orders so that the account
                    # balance is correctly updated.
                    account.pending_orders[server_order.id] = lost_order
                    account.cancel_order(server_order.id)
                    order_found = True
                    break

            # The order was not found on the server. Refund any captured balance.
            if not order_found:
                account.balance[lost_order.currency] += lost_order.captured

    save_state()

    exit_code = 1
    try:
        trade_loop(app_state, accounts, twitter_client, twitter_handles)
    except (KeyboardInterrupt, InterruptedError):
        log.info("Stopping bot...")
        exit_code = 0
    except Exception as exc:
        log.exception("Caught unhandled exception: %s", exc)
    finally:
        save_state()
        log.info("State has been saved. Exiting...")
        sys.exit(exit_code)


def main():
    """ Entry point """
    # Print file's docstring if -h is invoked
    parser = argparse.ArgumentParser(description=_help_intro,
                                     formatter_class=help_formatter)
    subparsers = parser.add_subparsers(
        help=('subcommands; add "-h" or "--help" after a subcommand for its '
              'parameters'), dest='subparser_name')

    trade_parser = subparsers.add_parser(
        'trade', help='trades based on tweets')

    # Add command-line arguments
    trade_parser.add_argument(
        '--profile', '-p', type=str, required=False, default='default',
        help='which profile to use for trading'
    )
    trade_parser.add_argument(
        '--config', '-c', type=str, required=False,
        default=os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'config', 'config.py'),
        help=('config file; this is Python that sets the variable "config" '
              'to a list of dictionaries')
    )
    trade_parser.add_argument(
        '--interval', '-i', type=float, required=False, default=905,
        help=('how long to wait (in s) before reattempting to connect '
              'after getting rate-limited')
    )
    trade_parser.add_argument(
        '--sleep', '-s', type=float, required=False, default=0.5,
        help='how long to wait (in s) after an order has been placed'
    )
    trade_parser.add_argument(
        '--state', type=str, required=False, default='state.dat',
        help='state file; this is where the bot keeps its runtime state'
    )
    args = parser.parse_args()

    if args.subparser_name == 'trade':
        trade(args)


if __name__ == '__main__':
    main()
