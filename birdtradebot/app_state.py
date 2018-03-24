import logging
import os

import jsonpickle

from .twitter import TwitterState
from .exchange import AccountState

log = logging.getLogger(__name__)


def load_app_state(path):
    try:
        with open(path, 'r') as fp:
            data = fp.read()
            state = jsonpickle.loads(data)
    except (ValueError, IOError, TypeError):
        log.warning("Could not load state from '%s'. Creating new...", path)
        state = AppState()

    return state


def save_app_state(path, state):
    try:
        with open(path + '.tmp', 'w') as fp:
            data = jsonpickle.dumps(state)
            fp.write(data)
    except (ValueError, IOError, TypeError) as err:
        log.critical("Could not save state: %s", err)

    try:
        os.rename(path + '.tmp', path)
    except OSError as ose:
        log.critical("Could not update saved state: %s", ose)


class AppState:
    def __init__(self):
        self.twitter: TwitterState = {}
        self.accounts: AccountState = {}
