import json
import ccxt

from ccxt.base.errors import (
        BaseError,
        InsufficientFunds,
        OrderNotFound)

from decimal import Decimal as D


def handle_errors(f):
    def wrapper(*args, **kwargs):
        err = {'type': 'error', 'message': 'could not determine error type'}
        try:
            return f(*args, **kwargs)
        except InsufficientFunds as ife:
            err['message'] = 'Insufficient funds'
            err['orig_message'] = '%s' % ife
        except OrderNotFound as nfe:
            err['message'] = 'Order not found'
            err['orig_message'] = '%s' % nfe
        except BaseError as be:
            msg = '%s' % be
            if 'message' not in msg:
                return err

            json_start = msg.find('{')
            if json_start == -1:
                return err

            maybe_json = msg[json_start:]
            try:
                json_err = json.loads(maybe_json)
            except (TypeError, ValueError):
                return err
            else:
                msg = json_err['message']
                if 'minimum size for' in msg:
                    err['orig_message'] = msg
                    msg = 'Order size is too small.'
                err['message'] = msg

        return err

    return wrapper


def convert_pair_from_gdax(gp):
    # The rules file uses "IOT" as IOTA's currency identifier (e.g., IOT-USD).
    # However, ccxt expects "IOTA" as the identifier.
    currency_map = {
        'IOT': 'IOTA'
    }
    left, right = gp.split('-', 1)
    pair = '%s/%s' % (currency_map.get(left, left), currency_map.get(right, right))
    return pair


def convert_raw_pair_to_gdax(bp):
    r = '%s-%s' % (bp[:3], bp[3:])
    return r.upper()


def convert_raw_type_to_gdax(bt):
    return bt.rsplit(' ', 1)[1]


def convert_gdax_order_to_bitfinex(gdax_order):
    order_type = gdax_order.get('type', 'limit')
    order = {
        'symbol': convert_pair_from_gdax(gdax_order['product_id']),
        'amount': gdax_order['size'], 
        'side': gdax_order['side'],
        'type': order_type,
    }
    if 'price' in gdax_order:
        order['price'] = gdax_order['price']
    post_only = gdax_order.get('post_only')
    if order_type == 'limit' and post_only:
        order['params'] = {
            'is_postonly': True
        }

    return order


def convert_bitfinex_order_reply_to_gdax(order):
    if order is None:
        return None
    if 'info' in order:
        raw_reply = order['info']
    else:
        raw_reply = order

    if raw_reply['is_live']:
        status = 'pending'
        settled = False
    elif raw_reply['is_cancelled']:
        status = 'done'
        settled = False
    else:
        status = 'done'
        settled = D(raw_reply['remaining_amount']) == D('0.0')

    reply = {
            'id': raw_reply['id'],
            'size': raw_reply['executed_amount'],
            'product_id': convert_raw_pair_to_gdax(raw_reply['symbol']),
            'side': raw_reply['side'],
            'type': convert_raw_type_to_gdax(raw_reply['type']),
            'status': status,
            'settled': settled,
            'created_at': order.get('datetime'),
    }
    price = raw_reply.get('price')
    if price is not None:
        reply['executed_value'] = str(D(raw_reply['executed_amount']) * D(price))
        reply['price'] = str(price)

    return reply


class GDAXInterfaceAdapter:
    '''
    Provides access to the Bitfinex exchange, using the GDAX API
    '''
    def __init__(self, key, secret, _):
        self.bitfinex = ccxt.bitfinex({'apiKey': key, 'secret': secret})

    def get_product_order_book(self, pair):
        r = self.bitfinex.fetch_order_book(convert_pair_from_gdax(pair), limit=1)
        return r

    @handle_errors
    def get_accounts(self):
        r = self.bitfinex.fetch_balance()
        currencies = r['info']
        gdax_currencies = {
                'BTC': False,
                'BCH': False,
                'ETH': False,
                'LTC': False,
                'EUR': False,
                'USD': False,
        }
        for c in currencies:
            c['currency'] = c['currency'].upper()
            if c['currency'] in gdax_currencies:
                gdax_currencies[c['currency']] = True

        for currency, found in gdax_currencies.items():
            if not found:
                currencies.append({'currency': currency, 'available': '0.0'})

        return currencies

    @handle_errors
    def get_order(self, order_id):
        o = self.bitfinex.fetch_order(order_id)
        return convert_bitfinex_order_reply_to_gdax(o)
 
    @handle_errors
    def cancel_order(self, order_id):
        r = self.bitfinex.cancel_order(order_id)
        return convert_bitfinex_order_reply_to_gdax(r)

    @handle_errors
    def buy(self, **gdax_order):
        order = convert_gdax_order_to_bitfinex(gdax_order)
        r = self.bitfinex.create_order(**order)
        return convert_bitfinex_order_reply_to_gdax(r)

    @handle_errors
    def sell(self, **gdax_order):
        order = convert_gdax_order_to_bitfinex(gdax_order)
        r = self.bitfinex.create_order(**order)
        return convert_bitfinex_order_reply_to_gdax(r)
