#!/usr/bin/env python
"""
birdtradebot

Checks tweets and
uses rules specified in file to make market trades on GDAX using
https://github.com/danpaquin/GDAX-Python. Default rules are stored in 
rules/birdpersonborg.py and follow the tweets of @birdpersonborg.
"""
from __future__ import print_function

import sys
import os
import errno
import time
import argparse
import getpass
import base64
import json
import decimal
import datetime

from copy import deepcopy

from decimal import Decimal as D

decimal.getcontext().prec = 8
decimal.getcontext().rounding = decimal.ROUND_DOWN

# For 2-3 compatibility
try:
    input = raw_input
except NameError:
    pass

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
        'birdtradebot requires PyCrypto. Install it with '
        '"pip install pycrypto".'
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
import re
import logging

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def help_formatter(prog):
    """ So formatter_class's max_help_position can be changed. """
    return argparse.HelpFormatter(prog, max_help_position=40)


def prettify_dict(rule):
    """ Prettifies printout of dictionary as string.

        rule: rule

        Return value: rule string
    """
    return json.dumps(rule, sort_keys=False,
                        indent=4, separators=(',', ': '))

def get_price(gdax_client, pair):
    """ Retrieve bid price for a pair

        gdax_client: instance of gdax.AuthenticatedClient
        pair: The pair that we want to know the price

        Return value: string with the pair bid price
    """
    order_book = gdax_client.get_product_order_book(pair)
    return D(order_book['bids'][0][0])

def get_balance(gdax_client, status_update=False, status_csv=False):
    """ Retrieve balance in user accounts

        gdax_client: instance of gdax.AuthenticatedClient
        status_update: True iff status update should be printed

        Return value: dictionary mapping currency to account information
    """
    balance = {}
    for account in gdax_client.get_accounts():
        balance[account['currency']] = D(account['available'])
    if status_update:
        balance_str = ', '.join('%s: %.8f' % (p, a) for p, a in balance.items())
        log.info('Current balance in wallet: %s' % balance_str)
    if status_csv:
        currentDT = datetime.datetime.now()
        # TODO - do this log for the pairs we are trading (retrieved from rules)
        balance_csv = "%s, balance, EUR-ETH-BTC, %s, %s, %s, bids, BTC-EUR ETH-EUR ETH-BTC, %s, %s, %s" % (currentDT.strftime("%Y-%m-%d %H:%M:%S"),
            '%.8f' % balance['EUR'],
            '%.8f' % balance['ETH'],
            '%.8f' % balance['BTC'],
            '%s' % get_price(gdax_client, 'BTC-EUR'),
            '%s' % get_price(gdax_client, 'ETH-EUR'),
            '%s' % get_price(gdax_client, 'ETH-BTC'))
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
            if 'User not found' in e.message:
                log.warning('Handle %s not found; skipping rule...' % handle)
            else:
                raise

    if not ids_map:
        raise RuntimeError('No followable Twitter handles found in rules!')

    return ids_map


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

        'handle': tweet['user']['screen_name'],
        'tweet_date': tweet['created_at'],
        'id': tweet['id_str'],

        'position': None,
        'status': None,
        'tries_left': retries + 1,
        'market_fallback': rule.get('market_fallback', False),
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

    def __init__(self, rules, gdax_client, twitter_client, handles, state,
                 sleep_time=0.5):
        self.rules = rules
        self.gdax = gdax_client
        self.twitter = twitter_client
        self.handles = handles
        self.state_obj = state
        self.state = state.d
        self.sleep_time = sleep_time
        self.available = get_balance(self.gdax, status_update=False)
        self.public_client = gdax.PublicClient() # for product order book

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

            if ctxt['status'] in ('settled', 'expired'):
                continue

            if ctxt['status'] == 'pending':
                r = self.gdax.get_order(ctxt['order_id'])
                log.debug("Fetched order %s status: %s", ctxt['order_id'], r)

                if r.get('status') in ('done', 'settled'):
                    ctxt['status'] = 'settled'
                    ctxt['position'] = \
                        'long' if ctxt['order']['side'] == 'buy' else 'short'
                    log.info("Order %s done: %s", ctxt['order_id'], r)
                    log.info("csv %s,%s,%s,%s,%s,%s,%s", 
                             r.get('done_at'), r.get('product_id'),
                             ctxt['position'], r.get('filled_size'), 
                             r.get('price'), 
                             r.get('executed_value'), r.get('type'))
                    self.available = get_balance(self.gdax, status_update=True, status_csv=True)
                    continue
                elif now < ctxt['retry_expiration']:
                    log.debug("Pending order %s has not yet expired: %s",
                              ctxt['order_id'], ctxt['order_instance'])
                    continue
                else:
                    log.info("Pending order expired. Tries left: %d, details: %s",
                             ctxt['tries_left'], ctxt['order_instance'])

            if ctxt['tries_left'] > 0:
                ctxt['tries_left'] -= 1
                ctxt['retry_expiration'] = now + ctxt['retry_ttl']
                self._place_order(ctxt)
            elif ctxt['market_fallback'] and ctxt['order']['type'] == 'limit':
                log.info("No more retries left, but market fallback is "
                         "enabled. Retrying one last time as market taker.")
                ctxt['order']['type'] = 'market'
                ctxt['retry_expiration'] = now + ctxt['retry_ttl']
                self._place_order(ctxt)
            else:
                log.warning("Order context expired. No more retries: %s", ctxt)
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

    def _get_pair_contexts(self, tweet, rule):
        contexts = []
        for order in rule['orders']:
            contexts.append(new_pair_context(rule, order, tweet))
        return contexts

    def _check_funds(self, r, base_asset, order):
        orig_order_size = order['size']
        for i in range(5):
            try:
                funding_error = 'insufficient funds' in r['message'].lower()
            except (TypeError, KeyError):
                break
            else:
                if not funding_error:
                    break

            base_asset_amount = self.available[base_asset]
            self.available = get_balance(self.gdax, status_update=True)
            if base_asset_amount != self.available[base_asset]:
                break

            log.warning("Fallback: server said we have insufficient funds. "
                        "Current balance (%s) == previous balance (%s). "
                        "Will try decreasing buy amount...",
                        base_asset_amount, self.available[base_asset])
            order['size'] = \
                '%.8f' % (D(orig_order_size) * (D('0.999') - D(i) * D('0.002')))

            log.info("Fallback: order: %s", order)
            r = self.gdax.buy(**order)
            log.info('Fallback: server reply: %s', r)
            time.sleep(self.sleep_time)

        log.debug("Fallback: done.")

        return r

    def _calc_buy_size(self, ctxt, asset, base_asset, ask, bid, price):
        order = ctxt['order']

        short_pairs = []
        for c in self.state['gdax']['contexts'].values():
            # Only makes sense for pairs with the same base asset
            # E.g., *-EUR, *-BTC
            if not c['pair'].endswith('-%s' % base_asset):
                continue
            if c['position'] == 'long':
                continue
            # TODO: check if this is right. It depends on wether GDAX updates
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
        return '%.8f' % size

    def _calc_sell_size(self, ctxt, ask, bid):
        order = ctxt['order']
        size = eval(order['size'].format(
                inside_ask=ask,
                inside_bid=bid,
                available=self.available
            ))
        return '%.8f' % size

    def _place_order(self, ctxt):
        # Ensure that there is no pending order for this context.
        if ctxt['status'] == 'pending':
            reply = self.gdax.cancel_order(ctxt['order_id'])
            if reply is None or 'error' in reply:
                log.error("Could not cancel order: %s", ctxt['order_id'])
                ctxt['status'] = 'error'
                return ctxt
        order_book = self.public_client.get_product_order_book(ctxt['pair'])
        inside_bid = D(order_book['bids'][0][0])
        inside_ask = D(order_book['asks'][0][0])

        # Create a new order from the order template
        order = deepcopy(ctxt['order'])
        ctxt['order_instance'] = order
        order['price'] = '%.2f' % eval(order['price'].format(
            inside_bid=inside_bid,
            inside_ask=inside_ask
        ))
        price = order['price']
        # Refresh balance
        self.available = get_balance(self.gdax, status_update=True)

        asset, base_asset = ctxt['pair'].split('-')

        if order['type'] == 'market' and 'price' in order:
            del order['price']

        if order['side'] == 'buy':
            order['size'] = self._calc_buy_size(
                ctxt, asset, base_asset, inside_ask, inside_bid, price)
            # TODO: If order size < 0.000010 don't place the order
            log.info('Placing order: %s' % order)
            r = self.gdax.buy(**order)
            r = self._check_funds(r, base_asset, order)
        else:
            assert order['side'] == 'sell'
            if self.available[asset] == 0:
                log.debug("Trying to go short with no available funds. "
                          "Finish order: %s", order)
                ctxt['status'] = 'settled'
                ctxt['position'] = 'short'
                return ctxt

            order['size'] = self._calc_sell_size(ctxt, inside_ask, inside_bid)
            log.info('Placing order: %s' % order)
            r = self.gdax.sell(**order)

        log.info('Order placed. Server reply: %s', r)
        time.sleep(self.sleep_time)

        if 'id' in r:
            ctxt['order_id'] = r['id']
            ctxt['status'] = r['status']
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

                is_settled = ctxt['status'] == 'settled'
                if new_ctxt['order']['side'] == ctxt['order']['side'] and is_settled:
                    log.warning(
                        "New pair context matches existing context. "
                        "Updating state without further actions. "
                        "Details: Existing order: %s, new order: %s",
                        ctxt['order'], new_ctxt['order'])
                    ctxt['id'] = new_ctxt['id']
                else:
                    ctxts[pair] = new_ctxt

    def run(self):
        next_status_ts = 0
        while True:
            now = time.time()
            self._run()
            if now > next_status_ts:
                self.available = get_balance(self.gdax, status_update=True, status_csv=True)
                next_status_ts = now + 3600
            time.sleep(120)


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
    config_parser = subparsers.add_parser(
                            'configure',
                            help=(
                                'creates profile for storing keys/secrets; '
                                'all keys are stored in "{}".'.format(
                                        os.path.join(
                                            os.path.expanduser('~'),
                                            '.birdtradebot',
                                            'config')
                                    )
                            )
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
    trade_parser.add_argument('--rules', '-r', type=str, required=False,
            default=os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                    'rules', 'birdpersonborg.py'),
            help=('rules file; this is Python that sets the variable "rules" '
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
    args = parser.parse_args()
    key_dir = os.path.join(os.path.expanduser('~'), '.birdtradebot')
    if args.subparser_name == 'configure':
        try:
            os.makedirs(key_dir)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        # Grab and write all necessary credentials
        config_file = os.path.join(key_dir, 'config')
        print('Enter a name for a new profile (default): ', end='')
        profile_name = input()
        if not profile_name: profile_name = 'default'
        salt = Random.new().read(AES.block_size)
        key = KDF.PBKDF2(getpass.getpass((
                'Enter a password for this profile. The password will be used '
                'to generate a key so all GDAX/Twitter passcodes/secrets '
                'written to {} are further encoded with AES256. '
                'You will have to enter a profile\'s password every time you '
                'run "birdtradebot trade": '
            ).format(config_file)), salt,
                dkLen=32, count=_key_derivation_iterations)
        previous_lines_to_write = []
        if os.path.exists(config_file):
            '''Have to check if the profile exists already. If it does, replace
            it. Assume the config file is under birdtradebot's control and thus 
            has no errors; if the user chooses to mess it up, that's on
            them.'''
            with open(config_file, 'rU') as config_stream:
                line = config_stream.readline().rstrip('\n')
                while line:
                    if line[0] == '[' and line[-1] == ']':
                        if profile_name == line[1:-1]:
                            # Skip this profile
                            for _ in range(8): config_stream.readline()
                            line = config_stream.readline().rstrip('\n')
                            continue
                        previous_lines_to_write.append(line)
                        for _ in range(8):
                            previous_lines_to_write.append(
                                        config_stream.readline().rstrip('\n')
                                    )
                    line = config_stream.readline().rstrip('\n')
        with open(config_file, 'w') as config_stream:
            print(''.join(['[', profile_name, ']']), file=config_stream)
        # Now change permissions
        try:
            os.chmod(config_file, 0o600)
        except OSError as e:
            if e.errno == errno.EPERM:
                print >>sys.stderr, (
                        ('Warning: could not change permissions of '
                         '"{}" so it\'s readable/writable by only the '
                         'current user. If there are other users of this '
                         'system, they may be able to read your credentials '
                         'file.').format(
                                config_file
                            )
                    )
                raise
        with open(config_file, 'a') as config_stream:
            print(''.join(['Salt: ', base64.b64encode(salt).decode()]),
                    file=config_stream)
            for token in ['GDAX key', 'GDAX secret', 'GDAX passphrase',
                            'Twitter consumer key', 'Twitter consumer secret',
                            'Twitter access token key',
                            'Twitter access token secret']:
                if 'key' in token:
                    print(''.join(['Enter ', token, ': ']), end='')
                    '''Write it in plaintext if it's a public key; then the 
                    user can open the config file and know which keys are in 
                    use.'''
                    print(''.join([token, ': ', input()]),
                            file=config_stream)
                else:
                    # A warning to developers in a variable name
                    unencoded_and_not_to_be_written_to_disk = getpass.getpass(
                                        ''.join(['Enter ', token, ': '])
                                    )
                    iv = Random.new().read(AES.block_size)
                    cipher = AES.new(key, AES.MODE_CFB, iv)
                    print(''.join([
                            token,
                            ' (AES256-encrypted using profile password): ',
                            base64.b64encode(iv + cipher.encrypt(
                                unencoded_and_not_to_be_written_to_disk
                            )).decode()]), file=config_stream)
            for line in previous_lines_to_write:
                print(line, file=config_stream)
        print(('Configured profile "{}". Encrypted credentials have been '
               'stored in "{}". '
               'Now use the "trade" subcommand to '
               'trigger trades with new tweets.').format(
                        profile_name,
                        config_file
                    ))
    elif args.subparser_name == 'trade':
        # Set and check rules
        from imp import load_source
        try:
            rules = load_source('rules', args.rules).rules
        except IOError as e:
            e.message = 'Cannot find or access rules file "{}".'.format(
                                                                    args.rules
                                                                )
            raise
        import copy
        # Add missing keys so listener doesn't fail
        new_rules = copy.copy(rules)
        order_vocab = frozenset([
            'client_oid', 'type', 'side', 'product_id', 'stp',
            'price', 'size', 'time_in_force', 'cancel_after',
            'post_only', 'funds', 'overdraft_enabled', 'funding_amount',
        ])

        for i, rule in enumerate(rules):
            # Check 'condition'
            try:
                eval(rule['condition'].format(
                        tweet='"The rain in Spain stays mainly in the plain."',
                        available={
                            'ETH' : .01,
                            'USD' : .01,
                            'LTC' : .01,
                            'BTC' : .01
                        }
                    ))
            except KeyError:
                # 'condition' isn't required, so make default True
                new_rules[i]['condition'] = 'True'
            except:
                raise RuntimeError(''.join([
                        ('"condition" from the following rule in the file '
                         '"{}" could not be '
                         'evaluated; check the format '
                         'and try again: ').format(args.rules),
                        os.linesep, prettify_dict(rule)
                    ])
                )

            # Check handles and keywords
            if 'handles' not in rule and 'keywords' not in rule:
                raise RuntimeError(''.join([
                        ('A rule must have at least one of {{"handles", '
                         '"keywords"}}, but this rule from the file "{}" '
                         'doesn\'t:').format(args.rules),
                        os.linesep, prettify_dict(rule)
                    ])
                )
            if 'handles' not in rule:
                new_rules[i]['handles'] = []
            if 'keywords' not in rule:
                new_rules[i]['keywords'] = []
            new_rules[i]['handles'] = [
                    handle.lower() for handle in new_rules[i]['handles']
                ]
            new_rules[i]['keywords'] = [
                    keyword.lower() for keyword in new_rules[i]['keywords']
                ]
            '''Validate order; follow https://docs.gdax.com/#orders for 
            filling in default values.'''
            if 'orders' not in rule or not isinstance(rule['orders'], list):
                raise RuntimeError(''.join([
                        ('Every rule must have an "orders" list, but '
                         'this rule from the file "{}" doesn\'t:').format(
                        args.rules), os.linesep, prettify_dict(rule)
                    ])
                )
            for j, order in enumerate(rule['orders']):
                if not isinstance(order, dict):
                    raise RuntimeError(''.join([
                        ('Every order must be a dictionary, but order #{} '
                         'from this rule in the file "{}" isn\'t:').format(
                        j+1, args.rules), os.linesep, prettify_dict(rule)]))
                unrecognized_keys = [
                        key for key in order if key not in order_vocab
                    ]
                if unrecognized_keys:
                    raise RuntimeError(''.join([
                        'In the file "{}", the "order" key(s) '.format(
                            args.rules),
                        os.linesep, '[',
                        ', '.join(unrecognized_keys), ']', os.linesep,
                        ('are invalid yet present in order #{} of '
                         'the following rule:').format(j+1),
                        os.linesep, prettify_dict(rule)
                    ]))
                try:
                    if order['type'] not in ('limit', 'market', 'stop'):
                        raise RuntimeError(''.join([
                            ('An order\'s "type" must be one of {{"limit", '
                             '"market", "stop"}}, which order #{} in this '
                             'rule from the file "{}" doesn\'t '
                             'satisfy:').format(j+1, args.rules),
                            os.linesep, prettify_dict(rule)
                        ]))
                except KeyError:
                    # GDAX default is limit
                    new_rules[i]['orders'][j]['type'] = 'limit'
                if 'side' not in order:
                    raise RuntimeError(''.join([
                            ('An order must have a "side", but order #{} in '
                             'this rule from the file "{}" doesn\'t:').format(
                             j+1, args.rules), os.linesep, prettify_dict(rule)
                        ])
                    )
                if order['side'] not in ['buy', 'sell']:
                        raise RuntimeError(''.join([
                            ('An order\'s "side" must be one of {{"buy", '
                             '"sell"}}, which order #{} in this rule '
                             'from the file "{}" doesn\'t satisfy:').format(
                             j+1, args.rules), os.linesep, prettify_dict(rule)
                        ])
                    )
                if 'product_id' not in order:
                    raise RuntimeError(''.join([
                            ('An order must have a "product_id", but in the '
                             'file "{}", order #{} from this rule '
                             'doesn\'t:').format(args.rules, j+1),
                            os.linesep, prettify_dict(rule)
                        ]))
                if new_rules[i]['orders'][j]['type'] == 'limit':
                    for item in ['price', 'size']:
                        if item not in order:
                            raise RuntimeError(''.join([
                                ('If an order\'s "type" is "limit", the order '
                                 'must specify a "{}", but in the file "{}",'
                                 'order #{} from this rule doesn\'t:').format(
                                 item, args.rules, j+1),
                                 os.linesep, prettify_dict(rule)
                            ]))
                elif new_rules[i]['orders'][j]['type'] in ['market', 'stop']:
                    if 'size' not in order and 'funds' not in order:
                        raise RuntimeError(''.join([
                                ('If an order\'s "type" is "{}", the order '
                                 'must have at least one of {{"size", '
                                 '"funds"}}, but in file "{}", order #{} '
                                 'of this rule doesn\'t:').format(
                                        new_rules[i]['orders'][j]['type'],
                                        args.rules, j+1
                                    ), os.linesep, prettify_dict(rule)]))
                for stack in ['size', 'funds', 'price']:
                    try:
                        eval(order[stack].format(
                            tweet=('"The rain in Spain stays mainly '
                                   'in the plain."'),
                            available={
                                'ETH' : .01,
                                'USD' : .01,
                                'LTC' : .01,
                                'BTC' : .01
                            }, inside_bid=200, inside_ask=200))
                    except KeyError:
                        pass
                    except Exception as e:
                        raise RuntimeError(''.join([
                                ('"{}" from order #{} in the following '
                                 'rule from the file "{}" could not be '
                                 'evaluated; check the format '
                                 'and try again:').format(
                                        stack, j+1, args.rules
                                    ), os.linesep, prettify_dict(rule)]))
        rules = new_rules
        # Use _last_ entry in config file with profile name
        key = None
        try:
            with open(os.path.join(key_dir, 'config'), 'rU') as config_stream:
                line = config_stream.readline().rstrip('\n')
                while line:
                    profile_name = line[1:-1]
                    if profile_name == args.profile:
                        salt = base64.b64decode(
                                config_stream.readline().rstrip(
                                        '\n').partition(': ')[2]
                            )
                        if key is None:
                            key = KDF.PBKDF2(getpass.getpass(
                                    'Enter password for profile "{}": '.format(
                                                                profile_name
                                                            )
                                ), salt,
                                dkLen=32, count=_key_derivation_iterations
                            )
                        keys_and_secrets = []
                        for _ in range(7):
                            item, _, encoded = config_stream.readline().rstrip(
                                                    '\n').partition(': ')
                            if 'key' in item:
                                # Not actually encoded; remove leading space
                                keys_and_secrets.append(encoded)
                                continue
                            encoded = base64.b64decode(encoded)
                            cipher = AES.new(
                                    key, AES.MODE_CFB,
                                    encoded[:AES.block_size]
                                )
                            keys_and_secrets.append(
                                    cipher.decrypt(
                                            encoded
                                        )[AES.block_size:]
                                )
                    else:
                        # Skip profile
                        for _ in range(8): config_stream.readline()
                    line = config_stream.readline().rstrip('\n')
        except IOError as e:
            e.message = (
                    'Cannot find birdtradebot config file. Use '
                    '"birdtradebot configure" to configure birdtradebot '
                    'before trading.'
                )
            raise

        # Get all twitter handles to monitor
        handles, keywords = set(), set()
        for rule in rules:
            handles.update(rule['handles'])
            keywords.update(rule['keywords'])

        try:
            # Instantiate GDAX and Twitter clients
            gdax_client = gdax.AuthenticatedClient(*keys_and_secrets[:3])
            twitter_client = Twython(*keys_and_secrets[3:7])

            # Are they working?
            get_balance(gdax_client, status_update=True)
            state = State('state.dat')
            trader = TradingStateMachine(rules, gdax_client, twitter_client,
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
            log.info('Rate limit error. Restarting in %d s...' % args.interval)
            time.sleep(args.interval)
