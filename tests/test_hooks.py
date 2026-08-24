"""Hook 事件系统：注册 / 触发 / 优先级 / pre 裁决聚合的单元测试。"""
import unittest

from core.hooks import HookContext, HookEvents, HookRegistry


class TestHookRegistry(unittest.TestCase):

    def setUp(self):
        # 每个用例用独立实例，避免相互污染
        self.reg = HookRegistry()
        self.ctx = HookContext()

    def test_register_and_fire(self):
        seen = []
        self.reg.register(HookEvents.STOP, lambda ctx: seen.append("x"))
        self.reg.fire(HookEvents.STOP, self.ctx)
        self.assertEqual(seen, ["x"])

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.register("no_such_event", lambda ctx: None)

    def test_priority_order(self):
        order = []
        self.reg.register(HookEvents.STOP, lambda c: order.append("low"), priority=100)
        self.reg.register(HookEvents.STOP, lambda c: order.append("high"), priority=-10)
        self.reg.register(HookEvents.STOP, lambda c: order.append("mid"), priority=0)
        self.reg.fire(HookEvents.STOP, self.ctx)
        self.assertEqual(order, ["high", "mid", "low"])

    def test_fire_context_shared(self):
        ctx = HookContext()
        ctx.state["flag"] = False
        self.reg.register(HookEvents.STOP, lambda c: c.state.update(flag=True))
        self.reg.fire(HookEvents.STOP, ctx)
        self.assertTrue(ctx.state["flag"])


class TestPreToolAggregation(unittest.TestCase):

    def setUp(self):
        self.reg = HookRegistry()
        self.ctx = HookContext()

    def _verdict(self):
        self.reg.fire_pre_tool(self.ctx)
        return self.ctx.verdict

    def test_no_hook_defaults_allow(self):
        self.assertEqual(self._verdict(), "allow")

    def test_none_means_allow(self):
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: None)
        self.assertEqual(self._verdict(), "allow")

    def test_confirm_overrides_allow(self):
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: "allow")
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: "confirm")
        self.assertEqual(self._verdict(), "confirm")

    def test_deny_overrides_confirm(self):
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: "confirm")
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: ("deny", "不安全"))
        self.assertEqual(self._verdict(), "deny")
        self.assertEqual(self.ctx.deny_reason, "不安全")

    def test_deny_short_circuits(self):
        calls = []
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: ("deny", "x"), priority=-1)
        # 第二个钩子（priority 更高）不应再被调用
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE,
                          lambda c: calls.append(1), priority=1)
        self.reg.fire_pre_tool(self.ctx)
        self.assertEqual(calls, [])  # deny 短路，后续钩子未执行

    def test_last_allow_wins_without_deny_confirm(self):
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: "confirm", priority=-1)
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE, lambda c: "allow", priority=1)
        self.assertEqual(self._verdict(), "confirm")  # confirm 优先于 allow

    def test_deny_reason_captured(self):
        self.reg.register(HookEvents.PRE_TOOL_EXECUTE,
                          lambda c: c.deny("工具被禁用"))
        self.assertEqual(self._verdict(), "deny")
        self.assertEqual(self.ctx.deny_reason, "工具被禁用")


if __name__ == "__main__":
    unittest.main()
