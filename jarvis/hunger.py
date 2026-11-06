"""Turn DeepSeek's account balance into a shared character state."""
import json
import os
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

from . import config


class BalanceUnavailable(RuntimeError):
    """The provider rejected a completion for insufficient balance."""


EMPTY_REPLY = '本鲸的脑力口粮见底了。DeepSeek 余额不足，补充后可以用 /hunger refresh 刷新，或者切回本地 Qwen。'


def classify_balance(payload, full):
    if not isinstance(payload, dict) or type(payload.get('is_available')) is not bool:
        raise ValueError('Invalid balance availability')
    if not payload['is_available']:
        return {'level': 'empty', 'value_01': 1.0}
    ratios = []
    for wallet in payload['balance_infos']:
        currency = wallet['currency']
        if currency not in full:
            continue
        amount = Decimal(str(wallet['total_balance']))
        if not amount.is_finite():
            raise ValueError('Invalid balance amount')
        ratios.append(max(Decimal(0), min(amount, full[currency])) / full[currency])
    if not ratios:
        raise ValueError('No supported wallet')
    hunger = 1 - max(ratios)
    level = 'hungry' if hunger >= Decimal('.75') else 'sated' if hunger == 0 else 'normal'
    return {'level': level, 'value_01': round(float(hunger), 4)}


class BalanceHunger:
    def __init__(self, *, fetch=None, clock=time.monotonic):
        try:
            self.full = {c: Decimal(os.getenv('DEEPSEEK_HUNGER_FULL_' + c, default))
                         for c, default in [('CNY', '20'), ('USD', '3')]}
        except InvalidOperation:
            raise ValueError('DEEPSEEK_HUNGER_FULL_CNY/USD must be positive finite amounts') from None
        if any(not n.is_finite() or n <= 0 for n in self.full.values()):
            raise ValueError('DEEPSEEK_HUNGER_FULL_CNY/USD must be positive finite amounts')
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.refresh_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.next_check = 0
        self.state = {'level': 'unknown', 'value_01': None}

    @staticmethod
    def _fetch():
        request = Request(config.DEEPSEEK_URL + '/user/balance', headers={
            'Authorization': 'Bearer ' + config.DEEPSEEK_API_KEY})
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def snapshot(self):
        with self.state_lock:
            return dict(self.state)

    def refresh(self, *, force=False):
        with self.refresh_lock:
            if not force and self.clock() < self.next_check:
                return self.snapshot()
            try:
                state = classify_balance(self.fetch(), self.full)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                state = {'level': 'unknown', 'value_01': None}
            self._store(state)
            return self.snapshot()

    def _store(self, state):
        with self.state_lock:
            self.state = state
        self.next_check = self.clock() + (60 if state['level'] == 'unknown' else 300)

    def mark_empty(self):
        with self.refresh_lock:
            self._store({'level': 'empty', 'value_01': 1.0})
