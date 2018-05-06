from typing import Dict, List

from utils import prettify_dict, D
from order import OrderTemplate


class Rule:
    def __init__(self, config: dict):
        self.config = config
        self.id: str = config.get('id')
        self.keywords: List[str] = config.get('keywords', [])
        self.condition = config.get('condition', None)
        self.order_ttl = int(config.get('order_ttl', 60))
        self.ttl = int(config.get('ttl', 600))
        self.check_interval = int(config.get('check_interval', 30))
        self.split_order_size = D(config.get('split_order_size', 0))
        self.tweet_ttl = int(config.get('tweet_ttl', 600))
        self.market_fallback: bool = config.get('market_fallback', False)
        self.position = None
        self.handles = config.get('handles', [])
        self.agreement_handles = config.get('agreement_handles', [])
        self.handles: List[str] = [
            h.lower() for h in self.handles
        ]
        self.agreement_handles: List[str] = [
            h.lower() for h in self.agreement_handles
        ]

        self._validate()
        self.order_template = OrderTemplate(config['order'])

    def _validate(self):
        if self.id is None:
            raise ValueError("A rule must have and id.")

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
            raise ValueError(
                '"condition" in rule %s could not be evaluated; check it '
                'and try again.' % prettify_dict(self.config)
            )

        # Check handles and keywords
        if not self.handles and not self.keywords:
            raise ValueError(
                'A rule must have at least one of "handles" or "keywords", '
                'but this rule does not: %s' % self.config
            )

        self.handles = [handle.lower() for handle in self.handles]
        self.keywords = [keyword.lower() for keyword in self.keywords]

        order = self.config.get('order')
        if order is None or not isinstance(order, dict):
            raise ValueError(
                'A rule must have an "order" and it must be a dictionary: %s' %
                self.config
            )
