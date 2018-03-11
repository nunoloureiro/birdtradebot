import os

from typing import Dict, List

from .utils import prettify_dict
from .order import OrderTemplate


class Rule:
    def __init__(self, config: dict):
        self.config = config
        self.handles: List[str] = config.get('handles', [])
        self.keywords: List[str] = config.get('keywords', [])
        self.condition: str = config.get('condition', True)
        self.retries: int = int(config['retries'])
        self.retry_ttl: int = int(config['retry_ttl'])
        self.tweet_ttl: int = int(config['tweet_ttl'])
        self.market_fallback: bool = config.get('market_fallback', False)
        self.order: Dict[str, str] = None
        self._validate()

    def _validate(self):
        # Check condition
        try:
            eval(self.condition.format(
                tweet='"The rain in Spain stays mainly in the plain."',
                available={
                    'ETH': .01,
                    'USD': .01,
                    'LTC': .01,
                    'BTC': .01
                }
            ))
        except Exception:
            raise RuntimeError(
                '"condition" in rule %s could not be evaluated; check it '
                'and try again.' % prettify_dict(self.config)
            )

        # Check handles and keywords
        if not self.handles and not self.keywords:
            raise RuntimeError(
                'A rule must have at least one of "handles" or "keywords", '
                'but this rule does not: %s' % self.config
            )

        self.handles = [handle.lower() for handle in self.handles]
        self.keywords = [keyword.lower() for keyword in self.keywords]

        order = self.config.get('order')
        if order is None or not isinstance(order, dict) :
            raise RuntimeError(
                'A rule must have an "order" and it must be a dictionary: %s' %
                self.config
            )
        self.order = OrderTemplate(order)