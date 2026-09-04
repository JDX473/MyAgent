"""工具注册表 / 白名单 / schema 生成 / run_tool 分发的单元测试。"""
import json
import unittest

import core.tools as tools


# 测试用：动态注册一个新工具（不污染全局白名单——用完后在 tearDown 清理）
@tools.tool
def _double(x: float) -> float:
    """把一个数字翻倍"""
    return x * 2


class TestToolRegistry(unittest.TestCase):

    def test_registered_into_registry_and_whitelist(self):
        # 注册的 _double 应同时进入注册表和执行白名单
        self.assertIn("_double", tools._TOOL_REGISTRY)
        self.assertIn("_double", tools._TOOL_WHITELIST)

    def test_whitelist_matches_registry(self):
        # 白名单应与注册表 key 集合一致
        self.assertEqual(set(tools._TOOL_REGISTRY.keys()), set(tools._TOOL_WHITELIST))

    def test_tool_with_custom_name(self):
        # @tool(name="别名") 的注册名用别名
        @tools.tool(name="custom_sum")
        def s(a: int, b: int) -> int:
            """相加"""
            return a + b

        try:
            self.assertIn("custom_sum", tools._TOOL_REGISTRY)
            self.assertIn("custom_sum", tools._TOOL_WHITELIST)
            self.assertNotIn("s", tools._TOOL_REGISTRY)
        finally:
            tools._TOOL_REGISTRY.pop("custom_sum", None)
            tools._TOOL_WHITELIST.discard("custom_sum")


class TestSchemaGeneration(unittest.TestCase):

    def test_schema_shape(self):
        s = tools.generate_tool_schema(_double)
        self.assertEqual(s["type"], "function")
        self.assertEqual(s["function"]["name"], "_double")
        self.assertEqual(s["function"]["description"], "把一个数字翻倍")
        params = s["function"]["parameters"]
        self.assertEqual(params["properties"]["x"]["type"], "number")
        self.assertEqual(params["required"], ["x"])

    def test_schema_excludes_return_annotation(self):
        # 返回值注解不应混入参数
        s = tools.generate_tool_schema(_double)
        self.assertNotIn("return", s["function"]["parameters"]["properties"])
        self.assertNotIn("return", s["function"]["parameters"]["required"])

    def test_type_mapping(self):
        self.assertEqual(tools._type_to_json(int), "integer")
        self.assertEqual(tools._type_to_json(float), "number")
        self.assertEqual(tools._type_to_json(str), "string")
        self.assertEqual(tools._type_to_json(bool), "boolean")
        self.assertEqual(tools._type_to_json(list), "string")  # 裸 list 兜底 string
        from typing import List
        # list[str] 应映射为字符串数组 schema
        self.assertEqual(
            tools._type_to_json(List[str]),
            {"type": "array", "items": {"type": "string"}},
        )

    def test_schema_respects_default_values(self):
        def f(a: int, b: int = 2, c: str = "x") -> str:
            """示例"""
            return str(a + b) + c

        s = tools.generate_tool_schema(f)
        self.assertEqual(s["function"]["parameters"]["required"], ["a"])
        self.assertIn("b", s["function"]["parameters"]["properties"])
        self.assertIn("c", s["function"]["parameters"]["properties"])


class TestRunTool(unittest.TestCase):

    def test_run_whitelisted_tool(self):
        r = json.loads(tools.run_tool("_double", '{"x": 21}'))
        self.assertEqual(r["result"], 42.0)

    def test_run_unregistered_not_whitelisted(self):
        # 未注册工具：即使硬塞进注册表也会被白名单拦截
        tools._TOOL_REGISTRY["sneaky"] = lambda: "should not run"
        try:
            r = json.loads(tools.run_tool("sneaky", "{}"))
            self.assertIn("blocked", r)
            self.assertIn("白名单", r["blocked"])
        finally:
            tools._TOOL_REGISTRY.pop("sneaky", None)

    def test_run_unknown_tool_blocked_by_whitelist(self):
        # 未注册的工具名：先被白名单拦截（blocked），不会走到"未注册"分支
        r = json.loads(tools.run_tool("nonexistent", "{}"))
        self.assertIn("blocked", r)
        self.assertIn("白名单", r["blocked"])

    def test_run_bad_arguments(self):
        # 参数不匹配 → 返回 error（而不是抛异常）
        r = json.loads(tools.run_tool("_double", '{}'))
        self.assertIn("error", r)
        self.assertIn("参数不匹配", r["error"])

    def test_registered_tools_includes_all(self):
        names = {s["function"]["name"] for s in tools.registered_tools()}
        self.assertIn("_double", names)


if __name__ == "__main__":
    unittest.main()
