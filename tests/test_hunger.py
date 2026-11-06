import unittest
from unittest.mock import Mock

from jarvis.agent import Agent
from jarvis.autonomy import InferenceGate, LocalPlanner
from jarvis.character_context import CharacterContext
from jarvis.hunger import BalanceHunger, BalanceUnavailable


def wallet(cny, usd=None):
    balances = [{'currency': 'CNY', 'total_balance': cny}]
    if usd is not None:
        balances.append({'currency': 'USD', 'total_balance': usd})
    return {'is_available': True, 'balance_infos': balances}


class HungerTests(unittest.TestCase):
    def setUp(self):
        from decimal import Decimal
        self.now = 0
        self.fetch = Mock(return_value=wallet('5'))
        self.meter = BalanceHunger(fetch=self.fetch, clock=lambda: self.now)
        self.meter.full = {'CNY': Decimal(20), 'USD': Decimal(3)}
        self.ctx = CharacterContext(hunger=self.meter)

    def test_wallet_selection_cache_and_invalid_reply(self):
        self.fetch.side_effect = [wallet('1', '1.50'), wallet('NaN'), wallet('20')]
        self.assertEqual(self.meter.refresh(), {'level': 'normal', 'value_01': .5})
        self.now = 299
        self.assertEqual(self.meter.refresh()['value_01'], .5)
        self.now = 300
        self.assertEqual(self.meter.refresh(), {'level': 'unknown', 'value_01': None})
        self.now = 360
        self.assertEqual(self.meter.refresh()['level'], 'sated')

    def test_low_balance_autonomy_reacts_once_and_recovers(self):
        model = Mock()
        model.chat.return_value = {'content': '{"decision":"wait"}'}
        planner = LocalPlanner(model, InferenceGate(), context=self.ctx)
        scene = {'snapshot': {'available_actions': ['set_expression']}}
        self.assertEqual(planner(scene)['expression']['emotion'], 'sad')
        self.assertNotIn('expression', planner(scene))
        self.fetch.return_value = wallet('20')
        self.meter.refresh(force=True)
        self.assertEqual(planner(scene)['expression']['emotion'], 'happy')
        model.chat.assert_not_called()
        planner(scene)
        model.chat.assert_called_once()

    def test_chat_receives_state_and_402_blocks_later_paid_calls(self):
        model = Mock(model='demo')
        model.chat.side_effect = BalanceUnavailable('HTTP 402')
        agent = Agent(model, Mock(schemas=[], desktop=None), context=self.ctx)
        self.assertIn('余额不足', agent.reply('你好'))
        prompt = model.chat.call_args.args[0][0]['content']
        self.assertIn('"hunger": {"level": "hungry"', prompt)
        self.assertNotIn('total_balance', prompt)
        self.assertIn('余额不足', agent.reply('还能聊天吗'))
        self.assertEqual(model.chat.call_count, 1)
        self.assertEqual(self.ctx.shared()['hunger']['level'], 'empty')

    def test_unavailable_wallet_and_network_failure_are_distinct(self):
        self.fetch.side_effect = [{'is_available': False}, OSError('failed')]
        self.assertEqual(self.meter.refresh()['level'], 'empty')
        self.assertIsNone(self.meter.refresh(force=True)['value_01'])
        self.assertNotIn('hunger', CharacterContext().shared())
