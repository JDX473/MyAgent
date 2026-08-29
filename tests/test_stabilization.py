"""JSON 稳定化(rcagent/stabilization.py)与 run_tool 参数解析兜底的单元测试。

覆盖:
  1. repair_json:直接可解析 / 围栏包裹 / 前后多余文字 / 单引号 / 垃圾输入
  2. run_tool:参数无法解析时返回 error(而不是抛异常崩溃)——这是对旧行为
     的一个隐式修复(旧代码 json.loads 抛 JSONDecodeError 未被捕获)
"""
import json
import unittest

from core.tools import run_tool
from rcagent.stabilization import repair_json


class TestRepairJson(unittest.TestCase):

    def test_valid_json_passthrough(self):
        self.assertEqual(repair_json('{"a": 1}'), {"a": 1})

    def test_code_fence_stripped(self):
        r = repair_json('我需要这样:\n```json\n{"a": 1, "b": [1, 2]}\n```\n以上')
        self.assertEqual(r, {"a": 1, "b": [1, 2]})

    def test_surrounded_by_noise(self):
        # 前后有多余文字:用括号配对提取出真正的 JSON
        r = repair_json('结果是 {"root_cause": "连接超时", "confidence": 0.9} 就这样')
        self.assertEqual(r["root_cause"], "连接超时")

    def test_single_quoted_keys_values(self):
        r = repair_json("{'a': 'x', 'b': 2}")
        self.assertEqual(r, {"a": "x", "b": 2})

    def test_garbage_returns_none(self):
        self.assertIsNone(repair_json("这完全不是 JSON"))

    def test_empty_returns_none(self):
        self.assertIsNone(repair_json(""))

    def test_non_dict_json_returns_none(self):
        # 只想修出 dict;数组/标量不算
        self.assertIsNone(repair_json("[1, 2, 3]"))

    def test_nested_and_unicode(self):
        r = repair_json("面对任意不转义的字符串 {\"证据\": \"SocketTimeoutException 已被逐字引用\"}")
        self.assertEqual(r["证据"], "SocketTimeoutException 已被逐字引用")


class TestRunToolParseFallback(unittest.TestCase):
    """run_tool 对"参数无法解析"应返回 error,而不是抛异常(修复旧行为)。"""

    def test_unparseable_arguments_return_error(self):
        r = json.loads(run_tool("websearch", '不是 json 的参数'))
        self.assertIn("error", r)
        self.assertIn("无法解析", r["error"])

    def test_default_bad_kwargs_still_error(self):
        # 参数可解析但函数签名不匹配 → 仍是"参数不匹配"
        from core.tools import run_tool as rt
        r = json.loads(rt("websearch", '{}'))
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()