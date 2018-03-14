#!/usr/bin/env python
"""
birdtradebot

Checks tweets and uses config specified in file to make market trades on
exchanges using a GDAX-like API. Configuration is stored in config/config.py
and follows the tweets of @birdpersonborg.
"""

import argparse
import decimal
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
from typing import List, Dict, Union, Callable

decimal.getcontext().rounding = decimal.ROUND_DOWN

_help_intro = """birdtradebot allows users to base trades on tweets."""

import twython

import logging

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from .utils import round_down, D, split_amount
from .exchange import (
    Account,
    AccountState,
    Pair,
    Exchange)
from .rule import Rule
from .twitter import Tweet, TwitterState
from .app_state import (
    load_app_state,
    save_app_state,
    AppState)
from .order import (
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
)

PRICE_PRECISION = {
    'ETH-EUR': 2,
    'BTC-EUR': 2,
    'ETH-BTC': 5,
    'IOT-USD': 4,
}


def help_formatter(prog):
    """ So formatter_class's max_help_position can be changed. """
    return argparse.HelpFormatter(prog, max_help_position=40)


def relevant_tweet(tweet, rule: Rule, balance: Dict[str, Decimal]):
    if (
            # Check if this is a user we are following
            (not rule.handles or tweet.screen_name and tweet.screen_name.lower() in rule.handles)

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


def calc_buy_size(account: Account, template: OrderTemplate,
                  ask: Decimal, bid: Decimal, price: Decimal) -> Decimal:
    _, quote_currency = template.product_id.split('-', 1)
    short_pairs = []
    for pair in account.pairs.values():
        # Only makes sense for pairs with the same quote currency
        # E.g., *-EUR, *-BTC
        if not pair.id.endswith('-%s' % quote_currency):
            continue

        if pair.position == 'long':
            continue

        # TODO: check if this is right. It depends on whether the exchange
        # updates the balance as orders are being placed or not
        if pair.position == 'short':  # and pair.status == 'settled' ?
            short_pairs.append(pair.id)
            continue

        # No position (short or long) yet. (i.e., no order has completed).
        buying = pair.rule.order_template.side == 'buy'
        selling = pair.rule.order_template.side == 'sell'

        if pair.status == 'expired':
            # The twitter bot went short.
            if selling: short_pairs.append(pair.id)
            # The twitter bot went long.
            if buying: pass

        # The order has not expired. So we are still changing our state.
        else:
            # Buying. Ensure we get our share of the available balance.
            if buying: short_pairs.append(pair.id)
            # Selling.
            if selling: pass

    log.debug("The following pairs are short: %s", short_pairs)
    n_short_pairs = len(short_pairs)
    if template.size == '{split_size}':
        size = account.balance[quote_currency] / D(n_short_pairs) / price
    else:
        max_size = account.balance[quote_currency] / price
        size = eval(template.size.format(
            inside_ask=ask,
            inside_bid=bid,
            balance=account.balance,
            max_size=max_size
        ))

    return D(round_down(size))


def calc_sell_size(account: Account, template: OrderTemplate, ask, bid):
    size = eval(template.size.format(
        inside_ask=ask,
        inside_bid=bid,
        balance=account.balance
    ))
    return D(round_down(size))


def new_order(account: Account, template: OrderTemplate) -> Order:
    # Update available funds before calculating order size.
    account.refresh_balance(status_update=True)

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
        size = calc_buy_size(account, template, inside_ask, inside_bid, price)
        order_dict['size'] = str(size)
    elif template.side == 'sell':
        size = calc_sell_size(account, template, inside_ask, inside_bid)
        order_dict['size'] = str(size)
    else:
        raise ValueError("Unrecognized order side: %s", template.side)

    log.info("Order built: %s", order_dict)

    return Order(order_dict)


def place_orders(action: Callable, orders: OrderBatch) -> OrderBatch:
    new_orders = list(orders.new)
    del orders.new[:]

    for order in new_orders:
        try:
            order_state: OrderState = action(order)
        except InsufficientFunds:
            log.error('Could not place order due to insufficient funds: %s',
                      order_to_dict(order))
            order.error = InsufficientFunds
            orders.error.append(order)
        except OrderSizeTooSmall:
            log.error('Could not place order because size is too small: %s',
                      order_to_dict(order))
            order.error = OrderSizeTooSmall
            orders.error.append(order)
        except OrderError:
            log.error("Unspecified order error: %s", order_to_dict(order))
            order.error = OrderError
            orders.error.append(order)
        else:
            if order_state.status == 'done':
                orders.done.append(order_state)
            else:
                orders.pending.append(order_state)
            time.sleep(round_down(0.1 + (random.random() % 0.2), 2))

    return orders


def check_pending_orders(account: Account, orders: OrderBatch) -> OrderBatch:
    pending = list(orders.pending)
    del orders.pending[:]

    for order in pending:
        try:
            state = account.get_order(order.id)
        except OrderNotFound:
            order.error = OrderNotFound
            orders.error.append(order)
        else:
            if state.status == 'done':
                orders.done.append(state)
            else:
                orders.pending.append(state)

    return orders


def split_order(order: Order, max_size: Decimal, truncate=5) -> List[Order]:
    if order.size > max_size:
        order_sizes = split_amount(order.size, max_size * 0.8, max_size)
    else:
        order_sizes = [order.size]

    orders = []
    for size in order_sizes[:truncate]:
        order_dict = order_to_dict(order, strict=True)
        order_dict['size'] = str(size)
        orders.append(Order(order_dict))

    return orders


def check_expired_orders(account: Account, orders: OrderBatch,
                         ttl: int) -> OrderBatch:
    now = int(time.time())
    pending = list(orders.pending)
    del orders.pending[:]

    for order in pending:
        if now < order.timestamp + ttl:
            orders.pending.append(order)
            continue

        try:
            account.cancel_order(order.id)
        except OrderError:
            log.error("Error cancelling order: %s", order_to_dict(order))

        order.error = OrderExpired
        orders.error.append(order)

    return orders


def limit_order_loop_step(account: Account, pair: Pair,
                          orders: OrderBatch) -> OrderBatch:

    main_order = new_order(account, pair.rule.order_template)

    orders = check_pending_orders(account, orders)
    orders = check_expired_orders(account, orders, pair.rule.order_ttl)

    # Honor original order size.
    if pair.size > 0:
        main_order.size = min(pair.size - pair.filled_size, D(main_order.size))
    else:
        pair.size = D(main_order.size)

    max_quote_currency = pair.rule.max_quote_currency
    if main_order.type == 'limit' and max_quote_currency > 0:
        max_order_size = max_quote_currency / main_order.price
    else:
        max_order_size = main_order.size

    action = account.buy if main_order.side == 'buy' else account.sell

    # Place new orders.
    orders.new = split_order(main_order, max_order_size, truncate=10)
    orders = place_orders(action, orders)

    return orders


def place_limit_order(account: Account, pair: Pair):
    expiration = int(time.time()) + pair.rule.ttl
    orders = OrderBatch()
    action_id = pair.tweet.id

    while True:
        now = int(time.time())

        if now > expiration:
            pair.status = 'expired'
            break
        if action_id != pair.tweet.id:
            break

        with account.lock:
            try:
                orders = limit_order_loop_step(account, pair, orders)
            except ValueError as ve:
                log.error("Order %s is not valid: %s",
                          pair.rule.order_template, ve)

        for order in orders.done:
            pair.filled_size += order.filled_size
            pair.executed_value += order.executed_value
            if not order.settled:
                log.warning("Order done but not settled: %s", order_to_dict(order))

        for order in orders.error:
            log.error("Order error: %s", order_to_dict(order))
            if order.error == InsufficientFunds:
                with account.lock:
                    try:
                        order_state = handle_insufficient_funds(account, order)
                    except OrderError:
                        log.warning("Order error: %s", order_to_dict(order))
                    else:
                        orders.pending.append(order_state)
            elif order.error == OrderExpired:
                log.warning("Order expired: %s", order_to_dict(order))
                pair.executed_value += order.executed_value
                pair.filled_size += order.filled_size
            else:
                log.warning("Error in order: %s", order_to_dict(order))

        del orders.done[:]
        del orders.error[:]

        if pair.size != 0 and pair.filled_size >= pair.size:
            pair.status = 'settled'
            break

        sleep_time = 10
        time.sleep(sleep_time)

    with account.lock:
        for order in orders.pending:
            log.warning("Cancelling pending order %s", order_to_dict(order))
            try:
                account.cancel_order(order.id)
            except OrderError:
                log.error("Error canceling order: %s", order_to_dict(order))


def place_market_order(account: Account, pair: Pair):
    template = copy.deepcopy(pair.rule.order_template)
    template.type = 'market'
    if template.post_only:
        template.post_only = None

    order = new_order(account, template)
    orders = OrderBatch()
    orders.new.append(order)
    action = account.buy if order.type == 'buy' else account.sell
    orders = place_orders(action, orders)

    # Market orders may take a few seconds to complete.
    for order_state in orders.pending:
        log.error("Placed market order. It should not have been pending: %s",
                  order_to_dict(order_state))

    if orders.done and orders.done[0].settled:
        pair.status = 'settled'
        return

    if orders.error and orders.error[0].error == InsufficientFunds:
        with account.lock:
            try:
                order_state = handle_insufficient_funds(account, order)
            except OrderError:
                log.warning("Order error: %s", order_to_dict(order))
            else:
                if order_state.settled:
                    pair.status = 'settled'
                    return

    pair.status = 'error'


def update_pair(account: Account, pair: Pair):
    _update_pair(account, pair)


def _update_pair(account: Account, pair: Pair):
    cancel_pending_orders(account)

    if pair.rule.order_template.type == 'limit':
        place_limit_order(account, pair)
        if pair.status == 'settled':
            return

    if pair.rule.order_template.type == 'fill_or_kill':
        if pair.status == 'settled':
            return

    if pair.rule.order_template.type == 'market' or pair.rule.market_fallback:
        with account.lock:
            place_market_order(account, pair)


def handle_insufficient_funds(account: Account,
                              order: Order) -> Union[OrderState, None]:
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
        order.size = round_down(size)
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

    def __init__(self, account: Account, state: AppState):
        self.account = account
        self.state = state

    def step(self, tweets: List[Tweet]):
        # Update state
        for tweet in tweets:
            self._update_pairs(tweet)

        for pair in self.account.pairs.values():
            t = threading.Thread(target=update_pair, args=(self.account, pair))
            t.daemon = True
            t.start()

    def _update_pairs(self, tweet: Tweet):
        log.debug("Got the following tweet: %s" % tweet.text)

        for rule in self.account.rules:
            if not relevant_tweet(tweet, rule, self.account.balance):
                continue

            # Relevant tweet. Do something with it...
            log.info("Rule match || @ %s: %s" % (tweet.screen_name, tweet.text))

            pair = self.account.pairs.get(rule.pair_id)
            if pair is None:
                pair = Pair(rule.pair_id)
            pair.update(rule, tweet)
            self.account.pairs[pair.id] = pair


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
            time.sleep(0.5)
            if not raw_tweets:
                break
            for t in raw_tweets:
                tweet = Tweet(t)
                twitter_state.update(tweet)
                tweets.append(tweet)

    return tweets


def cancel_pending_orders(account: Account):
    for order_id in account.pending_orders:
        try:
            account.cancel_order(order_id)
        except OrderError as oe:
            log.error("Error cancelling order: %s", oe)
        else:
            log.warning("Cancelled pending order %s on account %s. "
                        "Cancelling...", order_id, account.name)


def trade_loop(app_state: AppState, accounts: Dict[str, Account], twitter_client,
               twitter_handles):
    next_status_ts = 0
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
            with account.lock:
                t = TradingStateMachine(account, app_state)
                t.step(tweets)

        if now > next_status_ts:
            for account in accounts.values():
                with account.lock:
                    account.refresh_balance(status_update=True, status_csv=True)
            next_status_ts = now + 3600

        time.sleep(sleep_seconds)


def trade(args):
    # Set and check config
    from imp import load_source
    try:
        config = load_source('config', args.config)
    except IOError as e:
        e.message = 'Cannot find or access config file "%s"' % args.config
        raise

    try:
        # Saved application state
        app_state = load_app_state(args.state)

        # Twitter client, and handles and keywords to monitor
        twitter_handles, twitter_keywords = set(), set()
        twitter_client = twython.Twython(**config.twitter)

        # Exchanges
        exchanges = {}
        for name, config in config.exchanges.items():
            exchanges[name] = Exchange(config)

        # Accounts
        accounts: Dict[str, Account] = {}
        for name, acct in config.accounts.items():
            rules = []
            for r in acct.rules:
                rule = Rule(r)
                twitter_handles.update(rule.handles)
                twitter_keywords.update(rule.keywords)
                rules.append(rule)

            account_state = app_state.accounts.get('name')
            exchange = exchanges[acct['exchange']]
            if account_state is None:
                account_state = AccountState(name, acct['initial_balance'])
            account = Account(exchange, rules, account_state)
            accounts[name] = account
    except Exception as exc:
        log.exception(exc)
        log.error('Chances are, this opaque error happened because you ',
                  'entered incorrect security credentials '
                  'when you were configuring birdtradebot.\n')
        sys.exit(1)

    log.info('Twitter/Exchange credentials verified.')

    # Cancel any pending orders that may have persisted if the bot was not
    # cleanly shut down.
    for account in accounts.values():
        with account.lock:
            cancel_pending_orders(account)

    exit_code = 1
    try:
        trade_loop(app_state, accounts, twitter_client, twitter_handles)
    except (KeyboardInterrupt, InterruptedError):
        log.info("Stopping bot...")
        exit_code = 0
    except Exception as exc:
        log.error("Caught unhandled exception: %s", exc)
    finally:
        save_app_state(args.state, app_state)
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
