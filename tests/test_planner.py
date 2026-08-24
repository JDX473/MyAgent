"""任务计划模块（planner）的单元测试。

覆盖：
  1. core/planner.py 状态机（纯函数）：转移合法性、note/reason 校验、
     顺序前沿、单活跃、重试上限、revise、序列化
  2. tools/planner_tools.py 工具分发（run_tool + 绑定 ctx）
  3. 权限钩子对计划工具放行（allow）
  4. 漂移计数器递增/清零
  5. loop 集成：mock chat_completion 脚本化，验证防漂移提醒注入
"""
import json
import unittest
from unittest import mock

from core import planner
from core.hooks import HookContext
from core.tools import run_tool
from tools import planner_tools as ptools
from tools.hooks_setup import _permission_check


def _tool_call(name: str, args: dict, cid: str = "call_x") -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


# ----------------------------------------------------------------------
# 1. 状态机（core/planner.py）
# ----------------------------------------------------------------------
class TestPlannerCore(unittest.TestCase):

    def test_new_plan_all_pending(self):
        p = planner.new_plan(["a", "b"])
        self.assertEqual([s["status"] for s in p["steps"]], ["pending", "pending"])
        self.assertEqual(p["steps"][0]["title"], "a")
        self.assertEqual(p["version"], 1)

    def test_pending_to_in_progress_ok(self):
        p = planner.new_plan(["a", "b"])
        ok, p2, msg = planner.apply_transition(p, 0, "in_progress")
        self.assertTrue(ok)
        self.assertEqual(p2["steps"][0]["status"], "in_progress")

    def test_skip_ahead_rejected(self):
        # step 1 还不是前沿（step 0 未终结）→ 不能进入执行中
        p = planner.new_plan(["a", "b"])
        ok, _, msg = planner.apply_transition(p, 1, "in_progress")
        self.assertFalse(ok)
        self.assertIn("前沿", msg)

    def test_two_in_progress_rejected(self):
        # 构造"step1 已在执行中"的计划（不一致状态），step0 再推进应被单活跃守卫拒绝
        p = planner.new_plan(["a", "b"])
        p["steps"][1]["status"] = "in_progress"
        ok, _, msg = planner.apply_transition(p, 0, "in_progress")
        self.assertFalse(ok)
        self.assertIn("不能同时推进", msg)

    def test_done_requires_note(self):
        p = planner.new_plan(["a"])
        planner.apply_transition(p, 0, "in_progress")
        ok, _, msg = planner.apply_transition(p, 0, "done")  # 无 note
        self.assertFalse(ok)
        self.assertIn("note", msg)
        ok, p2, _ = planner.apply_transition(p, 0, "done", note="验证通过")
        self.assertTrue(ok)
        self.assertEqual(p2["steps"][0]["status"], "done")

    def test_failed_requires_reason(self):
        p = planner.new_plan(["a"])
        planner.apply_transition(p, 0, "in_progress")
        ok, _, msg = planner.apply_transition(p, 0, "failed")
        self.assertFalse(ok)
        self.assertIn("reason", msg)

    def test_failed_then_retry_or_skip(self):
        p = planner.new_plan(["a"])
        planner.apply_transition(p, 0, "in_progress")
        planner.apply_transition(p, 0, "failed", reason="权限被拒")
        # failed -> pending 重试
        ok, p2, _ = planner.apply_transition(p, 0, "pending")
        self.assertTrue(ok)
        self.assertEqual(p2["steps"][0]["status"], "pending")
        # failed -> skipped 跳过
        planner.apply_transition(p, 0, "skipped", reason="换方案")
        self.assertEqual(p["steps"][0]["status"], "skipped")

    def test_attempt_cap_blocks_endless_retry(self):
        p = planner.new_plan(["a"])
        # 完整重试链是 failed -> pending -> in_progress（进入执行中计一次尝试）
        for _ in range(planner.MAX_ATTEMPTS - 1):
            planner.apply_transition(p, 0, "in_progress")
            planner.apply_transition(p, 0, "failed", reason="再试")
            planner.apply_transition(p, 0, "pending")
        # 第三次进入执行中
        ok, _, _ = planner.apply_transition(p, 0, "in_progress")
        self.assertTrue(ok)
        planner.apply_transition(p, 0, "failed", reason="再试")
        # 已到上限：failed -> pending 应被拒绝
        ok, _, msg = planner.apply_transition(p, 0, "pending")
        self.assertFalse(ok)
        self.assertIn("重试", msg)

    def test_terminal_states_have_no_transition(self):
        p = planner.new_plan(["a"])
        planner.apply_transition(p, 0, "in_progress")
        planner.apply_transition(p, 0, "done", note="ok")
        ok, _, _ = planner.apply_transition(p, 0, "pending")
        self.assertFalse(ok)  # done 是终结态

    def test_revise_plan_preserves_done_steps(self):
        p = planner.new_plan(["a", "b", "c"])
        planner.apply_transition(p, 0, "in_progress")
        planner.apply_transition(p, 0, "done", note="ok")
        p2 = planner.revise_plan(p, ["b", "a", "d"])  # 重排 + 新增 d
        titles = [s["title"] for s in p2["steps"]]
        self.assertEqual(titles, ["b", "a", "d"])
        # 同名 step "a" 保留 done
        a = next(s for s in p2["steps"] if s["title"] == "a")
        self.assertEqual(a["status"], "done")
        # 新增的 "d" 是 pending
        d = next(s for s in p2["steps"] if s["title"] == "d")
        self.assertEqual(d["status"], "pending")
        self.assertGreater(p2["version"], p["version"])

    def test_serialize_and_done(self):
        p = planner.new_plan(["a"])
        planner.apply_transition(p, 0, "in_progress")
        planner.apply_transition(p, 0, "done", note="ok")
        self.assertTrue(planner.plan_done(p))
        text = planner.serialize_plan(p)
        self.assertIn("[✓]", text)
        self.assertIn("ok", text)
        self.assertEqual(planner.summarize_incomplete(p), "")

    def test_summarize_incomplete(self):
        p = planner.new_plan(["a", "b"])
        planner.apply_transition(p, 0, "in_progress")
        rest = planner.summarize_incomplete(p)
        self.assertIn("a", rest)
        self.assertIn("未完成", rest)


# ----------------------------------------------------------------------
# 2. 计划工具分发（run_tool + ctx 绑定）
# ----------------------------------------------------------------------
class TestPlannerTools(unittest.TestCase):

    def setUp(self):
        self.ctx = HookContext()
        ptools.bind(self.ctx)

    def _run(self, name, args) -> dict:
        # run_tool 返回 {"result": <工具返回值JSON字符串>}；工具返回值本身也是 JSON 字符串
        outer = json.loads(run_tool(name, json.dumps(args, ensure_ascii=False)))
        inner = json.loads(outer["result"]) if "result" in outer else outer
        return inner

    def test_plan_task_stores_plan(self):
        r = self._run("plan_task", {"steps": ["读", "写", "验"]})
        self.assertTrue(r.get("ok"))
        self.assertIn("plan", self.ctx.state)
        self.assertEqual(len(self.ctx.state["plan"]["steps"]), 3)

    def test_plan_task_empty_rejected(self):
        r = self._run("plan_task", {"steps": []})
        self.assertIn("error", r)
        self.assertNotIn("plan", self.ctx.state)

    def test_update_step_no_plan(self):
        r = self._run("update_step", {"step_index": 0, "status": "done", "note": "x"})
        self.assertIn("error", r)
        self.assertIn("plan_task", r["error"])

    def test_update_step_illegal_transition(self):
        self._run("plan_task", {"steps": ["a", "b"]})
        # 直接标记 step 1 done（跳步）→ 非法
        r = self._run("update_step", {"step_index": 1, "status": "done", "note": "x"})
        self.assertIn("error", r)
        # 未知状态
        r = self._run("update_step", {"step_index": 0, "status": "完成一半"})
        self.assertIn("error", r)

    def test_update_step_happy_path(self):
        self._run("plan_task", {"steps": ["a"]})
        r = self._run("update_step", {"step_index": 0, "status": "执行中"})
        self.assertTrue(r.get("ok"))
        r = self._run("update_step", {"step_index": 0, "status": "done", "note": "完成了"})
        self.assertTrue(r.get("ok"))
        self.assertEqual(self.ctx.state["plan"]["steps"][0]["status"], "done")

    def test_revise_plan(self):
        self._run("plan_task", {"steps": ["a", "b"]})
        r = self._run("revise_plan", {"steps": ["x", "y", "z"]})
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(self.ctx.state["plan"]["steps"]), 3)

    def test_get_plan(self):
        self._run("plan_task", {"steps": ["a"]})
        r = self._run("get_plan", {})
        self.assertTrue(r.get("ok"))
        self.assertIn("a", r["message"])

    def test_get_plan_without_plan(self):
        r = self._run("get_plan", {})
        self.assertIn("error", r)


# ----------------------------------------------------------------------
# 3. 权限钩子对计划工具放行
# ----------------------------------------------------------------------
class TestPlannerPermission(unittest.TestCase):

    def _verdict(self, tool: str, args: dict):
        ctx = HookContext()
        ctx.tool_name = tool
        ctx.arguments = json.dumps(args)
        return _permission_check(ctx)

    def test_plan_tools_allow(self):
        for tool, args in [
            ("plan_task", {"steps": ["a"]}),
            ("update_step", {"step_index": 0, "status": "done", "note": "x"}),
            ("revise_plan", {"steps": ["b"]}),
            ("get_plan", {}),
        ]:
            with self.subTest(tool=tool):
                result = self._verdict(tool, args)
                self.assertIn(result, (None, "allow"))

    def test_plan_tools_not_confirm_or_deny(self):
        ctx = HookContext()
        ctx.tool_name = "update_step"
        ctx.arguments = json.dumps({"step_index": 0, "status": "done", "note": "x"})
        result = _permission_check(ctx)
        self.assertNotIn(result, ("confirm", "deny"))


# ----------------------------------------------------------------------
# 4. 漂移计数器（钩子逻辑）
# ----------------------------------------------------------------------
class TestDriftCounter(unittest.TestCase):
    """直接调用钩子回调验证计数逻辑（不跑 loop）。"""

    def setUp(self):
        from tools import hooks_setup as hs
        self.counter = hs._on_plan_drift_counter

    def _mk_ctx(self, last_tool_calls=None, plan=True, drift=0):
        ctx = HookContext()
        if plan:
            ctx.state["plan"] = planner.new_plan(["a"])
        if drift:
            ctx.state["plan_drift"] = drift
        if last_tool_calls is not None:
            ctx.messages.append({"role": "assistant", "tool_calls": last_tool_calls})
        return ctx

    def test_increments_on_non_plan_tool(self):
        ctx = self._mk_ctx(last_tool_calls=[_tool_call("read", {"path": "x"})])
        self.counter(ctx)
        self.assertEqual(ctx.state["plan_drift"], 1)

    def test_resets_on_plan_tool(self):
        ctx = self._mk_ctx(last_tool_calls=[_tool_call("update_step", {"step_index": 0, "status": "done", "note": "x"})], drift=5)
        self.counter(ctx)
        self.assertEqual(ctx.state["plan_drift"], 0)

    def test_ignored_without_plan(self):
        ctx = self._mk_ctx(plan=False, last_tool_calls=[_tool_call("read", {"path": "x"})])
        self.counter(ctx)
        self.assertNotIn("plan_drift", ctx.state)


# ----------------------------------------------------------------------
# 5. loop 集成：防漂移提醒注入
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 5. loop 集成：防漂移提醒注入
# ----------------------------------------------------------------------
class TestPlanProgressPrint(unittest.TestCase):
    """计划工具执行后，POST_TOOL_EXECUTE 钩子应把当前计划打印到终端。"""

    def test_plan_progress_printed_after_plan_tool(self):
        from tools import hooks_setup as hs

        ctx = HookContext()
        ctx.state["plan"] = planner.new_plan(["读", "改"])
        ctx.tool_name = "update_step"
        # 让 step0 处于执行中，模拟一次状态更新
        planner.apply_transition(ctx.state["plan"], 0, "in_progress")

        with mock.patch("sys.stdout") as stdout:
            hs._on_plan_progress(ctx)
        # 打印里应包含当前计划的全貌（含 step 标题与状态）
        printed = stdout.write.call_args_list
        text = "".join(call[0][0] for call in printed if call and call[0])
        self.assertIn("当前计划", text)
        self.assertIn("读", text)
        self.assertIn("改", text)

    def test_plan_progress_ignores_non_plan_tool(self):
        from tools import hooks_setup as hs

        ctx = HookContext()
        ctx.state["plan"] = planner.new_plan(["读"])
        ctx.tool_name = "read"
        with mock.patch("sys.stdout") as stdout:
            hs._on_plan_progress(ctx)
        self.assertEqual(stdout.write.call_count, 0)


class TestLoopPlanReminder(unittest.TestCase):
    """mock chat_completion，脚本化模型行为，验证提醒在阈值后注入。"""

    def test_reminder_injected_after_drift_threshold(self):
        from core import loop as loop_mod

        script = [
            _tool_call("plan_task", {"steps": ["读", "改", "验"]}, cid="c1"),  # 1
            _tool_call("get_environment", {}, cid="c2"),                       # 2
            _tool_call("get_environment", {}, cid="c3"),                       # 3
            _tool_call("get_environment", {}, cid="c4"),                       # 4
            _tool_call("get_environment", {}, cid="c5"),                       # 5
            _tool_call("get_environment", {}, cid="c6"),                       # 6
            _tool_call("get_environment", {}, cid="c7"),                       # 7
            _tool_call("update_step", {"step_index": 0, "status": "in_progress"}, cid="c8"),  # 8
            None,                                                              # 9 最终回答
        ]
        seen_messages = []

        def fake_chat(messages, tools=None):
            seen_messages.append([dict(m) for m in messages])
            idx = len(seen_messages) - 1
            tc = script[idx] if idx < len(script) else None
            if tc is None:
                return {"choices": [{"message": {"role": "assistant", "content": "全部完成"}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            session = loop_mod.AgentSession()
            answer = session.chat("把这三步任务跑完", max_steps=20)

        self.assertEqual(answer, "全部完成")
        # 至少有一次模型请求里带着防漂移提醒
        injected = any(
            any(m.get("role") == "system" and "防漂移提醒" in m.get("content", "")
                for m in msgs)
            for msgs in seen_messages
        )
        self.assertTrue(injected, "防漂移提醒未被注入")
        # 提醒注入应发生在 get_plan 等非计划轮次之后：验证它出现在某次请求中，
        # 且注入后模型很快回到计划轨道（脚本第 8 次调用是 update_step）
        self.assertEqual(len(seen_messages), 9)

    def test_no_reminder_when_no_plan(self):
        from core import loop as loop_mod

        seen = []

        def fake_chat(messages, tools=None):
            seen.append([dict(m) for m in messages])
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            session = loop_mod.AgentSession()
            session.chat("你好", max_steps=5)

        all_content = " ".join(
            m.get("content", "") for msgs in seen for m in msgs if m.get("role") == "system"
        )
        self.assertNotIn("防漂移提醒", all_content)


# ----------------------------------------------------------------------
# 6. 自动续跑：单段预算用尽但计划未完成 → 注入提醒继续
# ----------------------------------------------------------------------
class TestLoopAutoContinue(unittest.TestCase):
    """mock chat_completion，验证自动续跑与总硬上限。"""

    def _run_session(self, calls, max_steps, env=None):
        """跑一个脚本化会话，返回 (answer, seen_messages)。"""
        from core import loop as loop_mod
        import os

        seen = []
        call_idx = [0]

        def fake_chat(messages, tools=None):
            seen.append([dict(m) for m in messages])
            i = call_idx[0]
            call_idx[0] += 1
            tc = calls[i] if i < len(calls) else None
            if tc is None:
                return {"choices": [{"message": {"role": "assistant", "content": "全部完成"}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        env = env or {}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            session = loop_mod.AgentSession()
            answer = session.chat("跑完计划", max_steps=max_steps)
        return answer, seen

    def test_auto_continue_when_plan_incomplete(self):
        # 单段 3 轮：plan_task + 2 个非计划工具，用完第 1 段
        calls = [
            _tool_call("plan_task", {"steps": ["a", "b", "c"]}, cid="p"),
            _tool_call("get_environment", {}, cid="g1"),
            _tool_call("get_environment", {}, cid="g2"),
            # 第 2 段开始（自动续跑注入后）
            _tool_call("get_environment", {}, cid="g3"),
            _tool_call("update_step", {"step_index": 0, "status": "done", "note": "ok"}, cid="u"),
            None,  # 最终回答
        ]
        answer, seen = self._run_session(calls, max_steps=3, env={"AGENT_MAX_STEPS": "3"})
        self.assertEqual(answer, "全部完成")
        # 总调用数 > 单段预算 3 → 确实续跑了
        self.assertEqual(len(seen), 6)
        # 第 2 段起的某次请求里带着"自动续跑"提醒
        auto = any(
            any(m.get("role") == "system" and "自动续跑" in m.get("content", "") for m in msgs)
            for msgs in seen
        )
        self.assertTrue(auto, "自动续跑提醒未被注入")

    def test_no_auto_continue_without_plan(self):
        # 没有计划：单段用尽即停，不续跑
        calls = [
            _tool_call("get_environment", {}, cid="g1"),
            _tool_call("get_environment", {}, cid="g2"),
            _tool_call("get_environment", {}, cid="g3"),  # 若续跑会用到
        ]
        answer, seen = self._run_session(calls, max_steps=2, env={"AGENT_MAX_STEPS": "2"})
        self.assertEqual(answer, "")
        self.assertEqual(len(seen), 2)  # 只跑单段，未续跑
        all_content = " ".join(
            m.get("content", "") for msgs in seen for m in msgs if m.get("role") == "system"
        )
        self.assertNotIn("自动续跑", all_content)

    def test_auto_continue_hard_cap(self):
        # 一直调用工具（永不结束）：总硬上限 = 单段 × 段数 = 2 × 2 = 4
        calls = [
            _tool_call("plan_task", {"steps": ["a"]}, cid="p"),
            _tool_call("get_environment", {}, cid="g1"),
            _tool_call("get_environment", {}, cid="g2"),
            _tool_call("get_environment", {}, cid="g3"),
            _tool_call("get_environment", {}, cid="g4"),  # 若超上限会用到
        ]
        answer, seen = self._run_session(
            calls, max_steps=2,
            env={"AGENT_MAX_STEPS": "2", "AGENT_MAX_TOTAL_STEPS": "2"})
        self.assertEqual(answer, "")  # 顶到硬上限
        self.assertEqual(len(seen), 4)  # 恰好 4 轮 = 硬上限，未突破

    def test_auto_continue_disabled(self):
        # AGENT_MAX_TOTAL_STEPS=0 → 关闭自动续跑，单段即上限
        calls = [
            _tool_call("plan_task", {"steps": ["a"]}, cid="p"),
            _tool_call("get_environment", {}, cid="g1"),
            _tool_call("get_environment", {}, cid="g2"),
        ]
        answer, seen = self._run_session(
            calls, max_steps=2,
            env={"AGENT_MAX_STEPS": "2", "AGENT_MAX_TOTAL_STEPS": "0"})
        self.assertEqual(answer, "")
        self.assertEqual(len(seen), 2)  # 只跑单段
        all_content = " ".join(
            m.get("content", "") for msgs in seen for m in msgs if m.get("role") == "system"
        )
        self.assertNotIn("自动续跑", all_content)


if __name__ == "__main__":
    unittest.main()
