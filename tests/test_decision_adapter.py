import fixture_runtime
import os,sys
from pathlib import Path
RELEASE=Path(os.environ['FEIYU_TEST_RELEASE'])
sys.path[:0]=[str(RELEASE/'src'),str(RELEASE/'astrbot')]
"""Offline contracts using the installed AstrBot API, without QQ or API calls."""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from p2d.decision_adapter import DecisionAdapter


class Checks(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, output):
        calls = []
        async def chat(**kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(completion_text=output)
        result = await DecisionAdapter().evaluate(
            types.SimpleNamespace(text_chat=chat),
            [(100, '群友A', '只有一天时间'), (101, '群友B', '肥鱼推荐路线吗？')])
        return result, calls

    async def test_contract_and_tool_isolation(self):
        result, calls = await self.evaluate('{"should_reply":true,"reason":"邀请"}')
        self.assertTrue(result['should_reply'])
        self.assertIsNone(calls[0]['func_tool'])
        self.assertEqual(calls[0]['contexts'], [])
        self.assertEqual(calls[0]['max_tokens'], 120)
        self.assertIn('只有一天时间', calls[0]['prompt'])
        self.assertNotIn('authorized-group', calls[0]['prompt'].split('最近消息')[1])

    async def test_invalid_output_fails_closed(self):
        for output in ('YES', '{}', 'null', '{"should_reply":false,"reason":"已解答"}'):
            result, _ = await self.evaluate(output)
            self.assertFalse(result['should_reply'])

    async def test_provider_failure_fails_closed(self):
        async def fail(**kwargs):
            raise RuntimeError('fixture unavailable')
        result = await DecisionAdapter().evaluate(types.SimpleNamespace(text_chat=fail), [])
        self.assertFalse(result['should_reply'])

    async def test_token_budget_does_not_mutate_chat_config(self):
        observed = []
        class Provider:
            provider_config = {'custom_extra_body': {'temperature': 0.2}}
            async def text_chat(self, **kwargs):
                observed.append(self.provider_config['custom_extra_body'].copy())
                return types.SimpleNamespace(completion_text='{"should_reply":false}')
        provider = Provider()
        await DecisionAdapter().evaluate(provider, [(100, '群友A', '完成了')])
        self.assertEqual(observed[0]['max_tokens'], 120)
        self.assertNotIn('max_tokens', provider.provider_config['custom_extra_body'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
