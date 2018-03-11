#!/usr/bin/env python
"""
birdtradebot

Checks tweets and
uses config specified in file to make market trades on exchanges using
the GDAX API. Default config are stored in
config/accounts.py and follow the tweets of @birdpersonborg.
"""
import argparse
import base64
import datetime
import decimal
import errno
import getpass
import json
import os
import random
import sys
import time

# Might be used in config
import math
import re

from copy import deepcopy
from decimal import Decimal
from math import floor
from typing import List, Dict, Set

from exchanges import bitfinex
from .rule import Rule

decimal.getcontext().rounding = decimal.ROUND_DOWN

_help_intro = """birdtradebot allows users to base GDAX trades on tweets."""
_key_derivation_iterations = 5000

try:
    import gdax
except ImportError as e:
    e.message = (
         'birdtradebot requires GDAX-Python. Install it with "pip install gdax".'
        )
    raise

try:
    import ccxt
except ImportError as e:
    e.message = (
         'birdtradebot requires ccxt. Install it with "pip install ccxt".'
        )
    raise

try:
    from twython import  Twython, TwythonError
except ImportError as e:
    e.message = (
            'birdtradebot requires Twython. Install it with '
            '"pip install twython".'
        )
    raise

try:
    from Crypto.Cipher import AES
    from Crypto.Protocol import KDF
    from Crypto import Random
except ImportError as e:
    e.message = (
        'birdtradebot requires PyCryptodome. Install it with '
        '"pip install pycryptodome".'
    )
    raise

try:
    import dateutil.parser
except ImportError as e:
    e.message = (
        'birdtradebot requires dateutil. Install it with '
        '"pip install python-dateutil".'
    )
    raise

# In case user wants to use regular expressions on conditions/funds
import logging

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


PRICE_PRECISION = {
    'ETH-EUR': 2,
    'BTC-EUR': 2,
    'ETH-BTC': 5,
    'IOT-USD': 4,
}


def D(n):
    """" Convert n to decimal """
    return Decimal(str(n))


def help_formatter(prog):
    """ So formatter_class's max_help_position can be changed. """
    return argparse.HelpFormatter(prog, max_help_position=40)


def round_down(n, d=8):
    d = int('1' + ('0' * d))
    return floor(n * d) / d


def get_price(gdax_client, pair):
    """ Retrieve bid price for a pair

        gdax_client: any object implementing the GDAX API
        pair: The pair that we want to know the price
        Return value: string with the pair bid price
    """
    try:
        order_book = gdax_client.get_product_order_book(pair)
    except KeyError:
        return 'NA'

    return D(order_book['bids'][0][0])


def get_balance(gdax_client, status_update=False, status_csv=False):
    """ Retrieve balance in user accounts

        gdax_client: any object implementing the GDAX API
        status_update: True iff status update should be printed

        Return value: dictionary mapping currency to account information
    """
    balance = {}
    for account in gdax_client.get_accounts():
        balance[account['currency']] = D(account['available'])
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
            get_price(gdax_client, 'BTC-EUR'),
            get_price(gdax_client, 'ETH-EUR'),
            get_price(gdax_client, 'ETH-BTC')
        )
        log.info('csv %s' % balance_csv)

    return balance


def relevant_tweet(tweet, rule, balance):
    if (
        # Check if this is a user we are following
        ((not rule['handles']) or
         ('user' in tweet and tweet['user']['screen_name'].lower() in rule['handles']))

        and

        # Check if the tweet text matches any defined condition
        ((not rule['keywords']) or
         any([keyword in tweet['text'].lower() for keyword in rule['keywords']]))

        and

        eval(rule['condition'].format(tweet='tweet["text"]',available=balance))):

        # Check if this is an RT or reply
        if (('retweeted_status' in tweet and tweet['retweeted_status']) or
                tweet['in_reply_to_status_id'] or
                tweet['in_reply_to_status_id_str'] or
                tweet['in_reply_to_user_id'] or
                tweet['in_reply_to_user_id_str'] or
                tweet['in_reply_to_screen_name']):

            return False

        return True

    return False


def twitter_handles_to_userids(twitter, handles):
    ids_map = {}

    for handle in handles:
        try:
            ids_map[handle] = twitter.show_user(screen_name=handle)['id_str']
        except TwythonError as e:
            msg = getattr(e, 'message', None)
            if msg is not None and 'User not found' in msg:
                log.warning('Handle %s not found; skipping rule...' % handle)
            else:
                raise

    if not ids_map:
        raise RuntimeError('No followable Twitter handles found in config!')

    return ids_map


def split_amount(amount, minval, maxval, precision=3):
    remaining = D(amount)
    parts = []
    while remaining > 0:
        n = D(random.random() * (maxval - minval) + minval)
        part = D(round_down(n, precision))
        part = D(min(remaining, part))
        parts.append(part)
        remaining -= part
    return parts


class State:
    def __init__(self, path):
        self.path = path
        self.d = self._load()

    def _load(self):
        try:
            with open(self.path, 'r') as fp:
                return json.load(fp)
        except (ValueError, IOError, TypeError):
            log.warning("Could not load state from '%s'. Creating new..." %
                        self.path)
            return {
                'twitter': {
                    'pairs': {},
                    'handles': {},
                },
                'gdax': {
                    'contexts': {},
                },
            }

    def save(self):
        with open(self.path, 'w') as fp:
            json.dump(self.d, fp)


def new_pair_context(rule, order, tweet):
    retries = int(rule.get('retries', 3))
    created = int(dateutil.parser.parse(tweet['created_at']).strftime('%s'))
    retry_ttl = int(rule.get('retry_ttl_s', 200))
    tweet_ttl = int(rule.get('tweet_ttl_s', 3600))

    pair = {
        'pair': order['product_id'],

        'order': order,
        'order_id': None,
        'order_instance': None,
        'order_result': None,
        'order_next_check': 0,

        'handle': tweet['user']['screen_name'],
        'tweet_date': tweet['created_at'],
        'id': tweet['id_str'],

        'position': None,
        'status': None,
        'tries_left': retries + 1,
        'market_fallback': rule.get('market_fallback', False),
        'cancel_expired': rule.get('cancel_expired', False),
        'early_exit': rule.get('early_exit'),
        'retry_ttl': retry_ttl,
        'retry_expiration': 0,
        'expiration': created + tweet_ttl,
    }

    if int(time.time()) > pair['expiration']:
        log.warning("Got new advice from an expired tweet. Ignoring... "
                    "Tweet date: %s, order: %s",
                    pair['tweet_date'], pair['order'])
        pair['status'] = 'expired'

    return pair


class TradingStateMachine:
    """ Trades on GDAX based on tweets. """

    def __init__(self, rules: List[Dict], gdax_client, public_client, twitter_client,
                 handles: Set, state: State, sleep_time: float=0.5):
        self.rules = rules
        self.gdax = gdax_client
        self.twitter = twitter_client
        self.handles = handles
        self.state_obj = state
        self.state = state.d
        self.sleep_time = sleep_time
        self.available = get_balance(self.gdax, status_update=False)
        self.public_client = public_client

    def _do_post_short_tasks(self, ctxt):
        if ctxt['status'] != 'settled' or ctxt['position'] != 'short':
            log.warning(
                "Order state does not allow to apply short config: %s", ctxt)
            return

        self._set_early_exit(ctxt)

    def _set_early_exit(self, ctxt):
        # "early_exit" specifies how much profit should we gain, before
        # exiting the current position.
        early_exit = ctxt.get('early_exit')
        if early_exit is None:
            return
        if early_exit.get('type') != 'short':
            return
        exit_size = early_exit.get('size')
        if exit_size is None:
            return
        exit_profit = early_exit.get('profit')
        if exit_profit is None:
            return
        sell_result = ctxt.get('order_result')
        if sell_result is None:
            log.warning("Could not determine previous order result: %s", ctxt)
            return

        executed_value = sell_result.get('executed_value')
        price = sell_result.get('price')
        size = sell_result.get('size')
        if executed_value is not None and size is not None:
            sell_price = D(executed_value) / D(size)
        elif price is not None:
            sell_price = D(price)
        else:
            log.warning('Could not determine sell price : %s', sell_result)
            return

        exit_size = D(exit_size)
        exit_profit = D(exit_profit)
        buy_price = sell_price - sell_price * exit_profit
        buy_size = sell_price * exit_size / buy_price
        precision = PRICE_PRECISION.get(ctxt['pair'], 2)
        order = {
            'side': 'buy',
            'type': 'limit',
            'post_only': True,
            'price': str(round_down(buy_price, precision)),
            'product_id': ctxt['order']['product_id'],
            'size': str(round_down(buy_size))
        }
        log.info("Placing buy back order: %s", order)
        self._place_order(ctxt, order=order)

    def _check_early_exit_status(self, ctxt, now):
        if ctxt['order_id'] is None or ctxt['position'] != 'short':
            return
        if now < ctxt.get('order_next_check', 0):
            return
        ctxt['order_next_check'] = now + 1800
        r = self.gdax.get_order(ctxt['order_id'])
        log.debug(
            "Fetched rebuy order %s details: %s", ctxt['order_id'], r)
        if r and r.get('status') == 'done':
            ctxt['order_id'] = None

    def _handle_expired_limit_order(self, ctxt, now):
        if ctxt['market_fallback']:
            log.info("No more retries left, but market fallback is "
                     "enabled. Retrying one last time as market taker.")
            ctxt['retry_expiration'] = now + ctxt['retry_ttl']
            self._place_order(ctxt, _type='market')
            return

        cancel = ctxt.get('cancel_expired', False)
        status_msg = ", but will be kept until new tweet arrives"
        if cancel:
            status_msg = " and cancelled"
            self._cancel_order(ctxt['order_id'])
            ctxt['order_id'] = None

        log.warning("Limit order expired%s: %s", status_msg, ctxt)
        ctxt['status'] = 'expired'

    def _handle_settled_order(self, ctxt, r):
        ctxt['status'] = 'settled'
        ctxt['position'] = 'long' if ctxt['order']['side'] == 'buy' else 'short'
        log.info("Order %s done: %s", ctxt['order_id'], r)
        log.info("csv %s,%s,%s,%s,%s,%s,%s,%s, %s",
                 r.get('done_at'), r.get('product_id'), r.get('side'),
                 r.get('filled_size'), r.get('price'), r.get('executed_value'),
                 r.get('type'), r.get('status'), r.get('fill_fees'))
        self.available = get_balance(self.gdax, status_update=True,
                                     status_csv=True)
        ctxt['order_id'] = None
        ctxt['order_result'] = r
        if ctxt['position'] == 'short':
            self._do_post_short_tasks(ctxt)

    def _run(self):
        twitter_state = self.state['twitter']
        gdax_state = self.state['gdax']
        ids_map = twitter_handles_to_userids(self.twitter, self.handles)
        tweets = []

        # Refresh Twitter bot positions
        for handle, uid in ids_map.items():
            try:
                latest_tweet = twitter_state['handles'][handle]['id']
            except KeyError:
                latest_tweet = None

            while True:
                new_tweets = self.twitter.get_user_timeline(
                    user_id=uid, exclude_replies=True, since_id=latest_tweet,
                    count=100)
                time.sleep(0.5)
                if not new_tweets:
                    break
                latest_tweet = new_tweets[0]['id_str']
                tweets += new_tweets

        # Prepare order contexts
        pair_contexts = gdax_state['contexts']
        for tweet in tweets:
            self._paper_trade(tweet, pair_contexts)
            self._update_twitter_state(
                tweet['user']['screen_name'],
                tweet['id_str'])

        # Issue orders for contexts
        for ctxt in pair_contexts.values():
            self._update_gdax_state(ctxt)
            self._update_twitter_state(ctxt['handle'], ctxt['id'], ctxt['pair'])

            now = int(time.time())

            if ctxt['status'] == 'expired':
                continue
            elif ctxt['status'] == 'settled':
                self._check_early_exit_status(ctxt, now)
                continue

            order_instance = ctxt.get('order_instance')
            try:
                limit_order = order_instance['type'] == 'limit'
            except (TypeError, KeyError):
                limit_order = False

            if ctxt['status'] in ('pending', 'open') and ctxt['order_id']:
                r = self.gdax.get_order(ctxt['order_id'])
                log.debug("Fetched order %s details: %s", ctxt['order_id'], r)

                if r.get('status') == 'done' and r['settled']:
                    self._handle_settled_order(ctxt, r)
                    continue

                elif now < ctxt['retry_expiration']:
                    log.debug("Pending order %s has not yet expired: %s",
                              ctxt['order_id'], order_instance)
                    continue

                else:
                    log.info("Pending order expired. Tries left: %d, details: %s",
                             ctxt['tries_left'], order_instance)

            if ctxt['tries_left'] > 0:
                ctxt['tries_left'] -= 1
                ctxt['retry_expiration'] = now + ctxt['retry_ttl']
                self._place_order(ctxt)
            elif limit_order:
                self._handle_expired_limit_order(ctxt, now)
            else:
                log.warning("Market order expired: %s", ctxt)
                ctxt['status'] = 'expired'

        self.state_obj.save()

    def _update_gdax_state(self, new_ctxt):
        state = self.state['gdax']
        pair = new_ctxt['pair']
        try:
            ctxt = state['contexts'][pair]
        except KeyError:
            state['contexts'][pair] = new_ctxt
        else:
            if new_ctxt['id'] > ctxt['id']:
                state['contexts'][pair] = new_ctxt

    def _update_twitter_state(self, handle, new_id, pair=None):
        handle = handle.lower()
        state = self.state['twitter']
        try:
            handle_id = state['handles'][handle]['id']
            if pair:
                pair_id = state['pairs'][pair]['id']
        except KeyError:
            if handle not in state:
                state['handles'][handle] = {
                    'id': new_id
                }
            if pair and pair not in state['pairs']:
                state['pairs'][pair] = {
                    'id': new_id
                }
            handle_id = state['handles'][handle]['id']
            if pair:
                pair_id = state['pairs'][pair]['id']

        if int(new_id) > int(handle_id):
            state['handles'][handle]['id'] = new_id
        if pair and int(new_id) > int(pair_id):
            state['pairs'][pair]['id'] = new_id

    def _cancel_order(self, order_id):
        if order_id is None:
            return
        log.info('Found previous order %s. Cancelling (if valid)...', order_id)
        reply = self.gdax.cancel_order(order_id)
        log.info('Server reply to order cancel request: %s', reply)
        # Wait a few of seconds for the order to be cancelled
        time.sleep(3)

    def _get_pair_contexts(self, tweet, rule):
        contexts = []
        for order in rule['orders']:
            ctxt = new_pair_context(rule, order, tweet)
            contexts.append(ctxt)
        return contexts

    def _check_buy_funds(self, r, base_asset, order):
        orig_order_size = order['size']
        for i in range(5):
            try:
                funding_error = 'insufficient funds' in r['message'].lower()
            except (TypeError, KeyError):
                break
            else:
                if not funding_error:
                    break

            previous_balance = self.available[base_asset]
            self.available = get_balance(self.gdax, status_update=True)
            log.warning("Fallback: server said we have insufficient funds. "
                        "Current balance: %s, previous balance: %s.",
                        self.available[base_asset], previous_balance)

            if previous_balance > self.available[base_asset]:
                break

            log.warning("Fallback: decreasing buy size...")

            size = D(orig_order_size) * (D('0.999') - D(i) * D('0.002'))
            size = str(round_down(size))
            order['size'] = size

            log.info("Fallback: order: %s", order)
            r = self.gdax.buy(**order)
            log.info('Fallback: server reply: %s', r)
            time.sleep(self.sleep_time)

        log.debug("Fallback: finished.")

        return r

    def _calc_buy_size(self, order, _, base_asset, ask, bid):
        short_pairs = []
        for c in self.state['gdax']['contexts'].values():
            # Only makes sense for pairs with the same base asset
            # E.g., *-EUR, *-BTC
            if not c['pair'].endswith('-%s' % base_asset):
                continue
            if c['position'] == 'long':
                continue
            # TODO: check if this is right. It depends on whether GDAX updates
            # the balance as orders are being placed or not
            if c['position'] == 'short':  # and c['status'] == 'settled' ?
                short_pairs.append(c['pair'])
                continue

            # No position (short or long) yet. (i.e., no order has completed).
            buying = c['order']['side'] == 'buy'
            selling = c['order']['side'] == 'sell'

            if c['status'] == 'expired':
                # The twitter bot went short.
                if selling: short_pairs.append(c['pair'])
                # The twitter bot went long.
                if buying: pass

            # The order has not expired. So we are still changing our state.
            else:
                # Buying. Ensure we get our share of the available balance.
                if buying: short_pairs.append(c['pair'])
                # Selling.
                if selling: pass

        log.debug("The following pairs are short: %s", short_pairs)
        n_short_pairs = len(short_pairs)
        price = order['price']
        if order['size'] == '{split_balance}':
            size = self.available[base_asset] / D(n_short_pairs) / D(price)
        else:
            base_asset_balance = self.available[base_asset]
            max_balance = base_asset_balance / D(price)
            size = eval(order['size'].format(
                inside_ask=ask,
                inside_bid=bid,
                available=self.available,
                max_balance=max_balance
            ))

        return str(round_down(size))

    def _calc_sell_size(self, order, ask, bid):
        size = eval(order['size'].format(
                inside_ask=ask,
                inside_bid=bid,
                available=self.available
            ))
        return str(round_down(size))

    def _build_order(self, order, ctxt):
        pair = ctxt['pair']
        asset, base_asset = pair.split('-')
        order_book = self.public_client.get_product_order_book(pair)
        inside_bid = D(order_book['bids'][0][0])
        inside_ask = D(order_book['asks'][0][0])
        precision = PRICE_PRECISION.get(pair, 2)
        price = D(eval(order['price'].format(
            inside_bid=inside_bid,
            inside_ask=inside_ask)))
        order['price'] = str(round_down(price, precision))
        if order['side'] == 'buy':
            order['size'] = self._calc_buy_size(
                order, asset, base_asset, inside_ask, inside_bid)
        elif order['side'] == 'sell':
            if self.available[asset] == 0:
                log.debug("Trying to go short with no available funds. "
                          "Finish order: %s", order)
                ctxt['status'] = 'settled'
                ctxt['position'] = 'short'
                return None
            order['size'] = self._calc_sell_size(order, inside_ask, inside_bid)
        else:
            raise RuntimeError("Unrecognized order side: %s", order['side'])

        return order

    def _place_order(self, ctxt, _type=None, order=None):
        # Ensure that there is no pending order for this context.
        if ctxt['order_id'] is not None:
            self._cancel_order(ctxt['order_id'])
            ctxt['order_id'] = None

        # Update available funds before calculating order size.
        self.available = get_balance(self.gdax, status_update=True)
        order = order if order is not None else deepcopy(ctxt['order'])
        order = self._build_order(order, ctxt)
        if order is None:
            return

        if _type is not None:
            order['type'] = _type
        if order['type'] == 'market':
            if 'price' in order:
                del order['price']
            if 'post_only' in order:
                del order['post_only']

        ctxt['order_instance'] = order

        asset, base_asset = ctxt['pair'].split('-')
        if order['side'] == 'buy':
            log.info('Placing order: %s' % order)
            r = self.gdax.buy(**order)
            r = self._check_buy_funds(r, base_asset, order)
        else:
            log.info('Placing order: %s' % order)
            r = self.gdax.sell(**order)

        log.info("csv %s,%s,%s,%s,%s,,%s,%s",
                 datetime.datetime.now(), order['product_id'],
                 order['side'], order['size'],
                 order.get('price', ''),
                 order['type'], ctxt['status'])
        log.info('Order placed. Server reply: %s', r)
        time.sleep(self.sleep_time)

        if 'id' in r:
            ctxt['order_id'] = r['id']
            if ctxt['status'] != 'settled':
                ctxt['status'] = r['status']
        else:
            msg = r.get('message')
            if msg is not None and 'order size is too small' in msg.lower():
                log.warning("Cannot place order because size is too small. "
                            "Order: %s, Server reply: %s", order, r)
                ctxt['status'] = 'expired'
            else:
                ctxt['status'] = 'error'

        return ctxt

    def _paper_trade(self, tweet, ctxts):
        log.debug("Got the following tweet: %s" % tweet['text'])

        for rule in self.rules:
            if not relevant_tweet(tweet, rule, self.available):
                continue

            # Relevant tweet. Do something with it...
            log.info("Tweet rule match || @ %s: %s" %
                     (tweet['user']['screen_name'], tweet['text']))

            new_ctxts = self._get_pair_contexts(tweet, rule)
            for new_ctxt in new_ctxts:
                pair = new_ctxt['pair']
                ctxt = ctxts.get(pair)
                if ctxt is None:
                    log.info("Potentially updating pair context [%s] based on "
                             "tweet %s... Note that this tweet has not yet been "
                             "validated (e.g., it may have expired). "
                             "Validation will occur shortly...",
                             tweet['id_str'], new_ctxt['order'])
                    ctxts[pair] = new_ctxt
                    continue

                # The new context must be more recent than the existing one.
                if int(new_ctxt['id']) <= int(ctxt['id']):
                    log.warning("Ignoring tweet with equal or older id "
                                "(our tweet: %s new tweet: >= %s)",
                                new_ctxt['id'], ctxt['id'])
                    continue

                log.debug("Updating context %s with %s", ctxt, new_ctxt)
                order_id = ctxt['order_id']
                new_ctxt['order_id'] = order_id
                ctxts[pair] = new_ctxt

    def run(self):
        next_status_ts = 0
        sleep_seconds = 120
        while True:
            now = time.time()
            try:
                self._run()
            except TwythonError as te:
                log.warning("Error fetching Twitter status: %s. "
                            "Will retry in %d s...", te, sleep_seconds)
            else:
                if now > next_status_ts:
                    self.available = get_balance(self.gdax, status_update=True, status_csv=True)
                    next_status_ts = now + 3600
            time.sleep(sleep_seconds)


def go():
    """ Entry point """
    # Print file's docstring if -h is invoked
    parser = argparse.ArgumentParser(description=_help_intro, 
                formatter_class=help_formatter)
    subparsers = parser.add_subparsers(help=(
                'subcommands; add "-h" or "--help" '
                'after a subcommand for its parameters'),
                dest='subparser_name'
            )

    trade_parser = subparsers.add_parser(
                            'trade',
                            help='trades based on tweets'
                        )
    # Add command-line arguments
    trade_parser.add_argument('--profile', '-p', type=str, required=False,
            default='default',
            help='which profile to use for trading'
        )
    trade_parser.add_argument('--config', '-c', type=str, required=False,
            default=os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                    'config', 'accounts.py'),
            help=('config file; this is Python that sets the variable "config" '
                  'to a list of dictionaries')
        )
    trade_parser.add_argument('--interval', '-i', type=float, required=False,
            default=905,
            help=('how long to wait (in s) before reattempting to connect '
                  'after getting rate-limited')
        )
    trade_parser.add_argument('--sleep', '-s', type=float, required=False,
            default=0.5,
            help='how long to wait (in s) after an order has been placed'
        )
    trade_parser.add_argument('--state', type=str, required=False,
            default='state.dat',
            help='state file; this is where the bot keeps its runtime state'
        )
    args = parser.parse_args()
    key_dir = os.path.join(os.path.expanduser('~'), '.birdtradebot')
    if args.subparser_name == 'trade':
        # Set and check config
        from imp import load_source
        try:
            config = load_source('config', args.config).rules
        except IOError as e:
            e.message = 'Cannot find or access config file "{}".'.format(
                                                                    args.rules
                                                                )
            raise

        # Get all twitter handles to monitor
        handles, keywords = set(), set()
        for rule in rules:
            handles.update(rule['handles'])
            keywords.update(rule['keywords'])

        exchange = os.getenv('EXCHANGE')
        exchanges = {
                'bitfinex': bitfinex.GDAXInterfaceAdapter
        }
        try:
            # Instantiate GDAX and Twitter clients
            twitter_client = Twython(*keys_and_secrets[3:7])
            if exchange is None:
                gdax_client = gdax.AuthenticatedClient(*keys_and_secrets[:3])
                public_client = gdax.PublicClient()  # for product order book
            else:
                gdax_client = exchanges[exchange](*keys_and_secrets[:3])
                public_client = gdax_client

            # Are they working?
            get_balance(gdax_client, status_update=True)
            state = State(args.state)
            trader = TradingStateMachine(
                rules, gdax_client, public_client, twitter_client,
                handles, state, sleep_time=args.sleep)

        except Exception:
            from traceback import format_exc
            log.error(format_exc())
            log.error(''.join(
                    [os.linesep,
                     'Chances are, this opaque error happened because either ',
                      os.linesep,
                      'a) You entered incorrect security credentials '
                      'when you were configuring birdtradebot.',
                      os.linesep,
                      'b) You entered the wrong password above.']
                ))
            exit(1)

        log.info('Twitter/GDAX credentials verified.')

        while True:
            log.info('Waiting for trades; hit CTRL+C to quit...')
            trader.run()
            time.sleep(args.interval)
