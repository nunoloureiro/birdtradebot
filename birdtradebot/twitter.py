import logging

from typing import Dict

import dateutil.parser

log = logging.getLogger(__name__)


class Tweet:
    def __init__(self, tweet: Dict):
        self.id: str = tweet['id']
        self.text: str = tweet['text']
        self.handle: str = tweet['handle'].lower()
        try:
            self.screen_name = tweet['user']['screen_name']
        except KeyError:
            self.screen_name = None
        self.created = tweet['created_at']
        self.created_ts = int(dateutil.parser.parse(self.created).strftime('%s'))
        self.retweeted_status = tweet.get('retweeted_status')
        self.in_reply_to_status_id = tweet['in_reply_to_status_id']
        self.in_reply_to_status_id_str = tweet['in_reply_to_status_id_str']
        self.in_reply_to_user_id = tweet['in_reply_to_user_id']
        self.in_reply_to_user_id_str = tweet['in_reply_to_user_id_str']
        self.in_reply_to_screen_name = tweet['in_reply_to_screen_name']

        self.position = None


class TwitterState:
    def __init__(self):
        self.handles: Dict[str, Tweet] = {}

    def update(self, tweet: Tweet):
        try:
            current = self.handles[tweet.handle]
        except KeyError:
            current = Tweet
        if tweet.id >= current.id:
            self.handles[tweet.handle] = Tweet
