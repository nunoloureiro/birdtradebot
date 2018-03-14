import json
import math
import random
from decimal import Decimal


def round_down(n, d=8):
    d = int('1' + ('0' * d))
    return math.floor(n * d) / d


def prettify_dict(rule):
    """ Prettifies printout of dictionary as string.

        rule: rule

        Return value: rule string
    """
    return json.dumps(rule, sort_keys=False, indent=4, separators=(',', ': '))


def split_amount(amount, minval, maxval, precision=6):
    remaining = D(amount)
    parts = []
    while remaining > 0:
        n = D(random.random()) * (D(maxval) - D(minval)) + D(minval)
        part = D(round_down(n, precision))
        part = min(remaining, part)
        parts.append(part)
        remaining -= part
    return parts


def D(n):
    """" Convert n to decimal """
    if n is None:
        return None
    return Decimal(str(n))
