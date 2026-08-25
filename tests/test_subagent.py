"""subAgent 工具（tools/subagent_tool.py）的单元测试。

覆盖：
  1. 注册：subagent 进注册表 + 白名单
  2. 工具集：subAgent 的 allowed 排除 plan 工具与自身（防递归）
  3. 嵌套 loop：subagent() 内部跑独立 AgentSession，返回结果
  4. 结果超长 → summary（最多 2 次）→ 仍超长才截断兜底
  5. allowed_tools 过滤：schema 只给允许工具；分发拦截不允许工具
"""
import json
import unittest
from unittest import mock

from core.loop import AgentSession, _filtered_tools
from core.tools import registered_tools, run_tool
from tools import subagent_tool as st
from tools.subagent_tool import _subagent_allowed


def _tool_call(name, args, cid="c1"):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _run_subagent(task: str) -> dict:
    """调用 subagent 工具并解两层 JSON（run_tool 外层 {"result":...} + 工具内层）。"""
    outer = json.loads(run_tool("subagent", json.dumps({"task": task})))
    if "result" in outer:
        return json.loads(outer["result"])
    return outer


class TestSubagentRegistry(unittest.TestCase):

    def test_registered_and_whitelisted(self):
        names = {s["function"]["name"] for s in registered_tools()}
        self.assertIn("subagent", names)
        from core.tools import _TOOL_WHITELIST
        self.assertIn("subagent", _TOOL_WHITELIST)

    def test_allowed_excludes_plan_and_self(self):
        allowed = _subagent_allowed()
        self.assertNotIn("subagent", allowed)  # 防递归
        self.assertFalse(allowed & {"plan_task", "update_step", "revise_plan", "get_plan"})
        # 正常工具可用
        self.assertTrue({"read", "write", "bash", "websearch"} <= allowed)

    def test_empty_task_rejected(self):
        r = _run_subagent("   ")
        self.assertIn("error", r)

    def test_non_string_task_rejected(self):
        """模型传非字符串 task（list/dict）应返回 error，不能崩溃。"""
        outer = json.loads(run_tool("subagent", json.dumps({"task": ["a", "b"]})))
        self.assertIn("error", json.loads(outer["result"]) if "result" in outer else outer)

    def test_non_string_name_falls_back(self):
        """name 非字符串时回退默认 "subAgent"，不崩溃。"""
        from core import loop as loop_mod

        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            outer = json.loads(run_tool("subagent", json.dumps({"task": "任务", "name": 123})))
        self.assertEqual(json.loads(outer["result"])["result"], "ok")

    def test_subagent_accepts_custom_name(self):
        """subagent 工具支持 name 参数，内层会话使用该名字。"""
        from core import loop as loop_mod

        captured = {}

        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            outer = json.loads(run_tool(
                "subagent",
                json.dumps({"task": "任务", "name": "web-researcher"}),
            ))
        self.assertEqual(json.loads(outer["result"])["result"], "ok")


class TestAllowedToolsFilter(unittest.TestCase):
    """core/loop.py 的 allowed_tools 过滤。"""

    def test_none_returns_all(self):
        self.assertEqual(len(_filtered_tools(None)), len(registered_tools()))

    def test_filters_to_allowed(self):
        filtered = _filtered_tools({"read", "write"})
        names = {s["function"]["name"] for s in filtered}
        self.assertEqual(names, {"read", "write"})

    def test_loop_rejects_disallowed_tool_at_dispatch(self):
        """分发双保险：模型请求了不在 allowed 的工具，返回 blocked。"""
        from core import loop as loop_mod

        calls = [
            _tool_call("bash", {"command": "ls"}),  # bash 不在 allowed 里
            None,
        ]
        seen = []

        def fake_chat(messages, tools=None):
            seen.append([dict(m) for m in messages])
            # 记录传给模型的工具 schema 名
            tool_names = [t["function"]["name"] for t in (tools or [])]
            seen[-1].append({"sent_tools": tool_names})
            i = len(seen) - 1
            tc = calls[i] if i < len(calls) else None
            if tc is None:
                return {"choices": [{"message": {"role": "assistant", "content": "完成"}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            sess = AgentSession()
            ans = sess.chat("跑一下", max_steps=3, allowed_tools={"read", "write"})

        # 1) schema 过滤：传给模型的只有 read/write
        sent = seen[0][-1]["sent_tools"]
        self.assertEqual(set(sent), {"read", "write"})
        self.assertNotIn("bash", sent)
        # 2) 分发拦截：bash 被 blocked（result 里含 blocked）
        tool_result = None
        for m in seen[-1]:
            if isinstance(m, dict) and m.get("role") == "tool":
                tool_result = m.get("content", "")
        self.assertIn("blocked", tool_result)
        self.assertIn("不在本子会话允许范围内", tool_result)


class TestSubagentExecution(unittest.TestCase):
    """subagent() 内部跑独立嵌套 loop。"""

    def test_subagent_runs_nested_loop_and_returns(self):
        from core import loop as loop_mod

        # subAgent 内部模型：直接回答（不调工具）
        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "子任务结果"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            r = _run_subagent("查一下")
        self.assertEqual(r["result"], "子任务结果")

    def test_subagent_passes_allowed_tools_to_inner_loop(self):
        """subAgent 内部 chat 的 allowed_tools 应排除 plan 和 subagent。"""
        from core import loop as loop_mod

        captured = {}

        def fake_chat(messages, tools=None):
            captured["tools"] = [t["function"]["name"] for t in (tools or [])]
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            run_tool("subagent", json.dumps({"task": "任务"}))

        # subAgent 内部模型看不到 plan 工具和 subagent
        self.assertNotIn("subagent", captured["tools"])
        self.assertNotIn("plan_task", captured["tools"])
        # 正常工具在
        self.assertIn("read", captured["tools"])


class TestSubagentSummary(unittest.TestCase):
    """超长结果 → summary（最多 2 次）→ 截断兜底。"""

    def _run(self, inner_responses):
        from core import loop as loop_mod

        seq = iter(inner_responses)

        def fake_chat(messages, tools=None):
            try:
                return next(seq)
            except StopIteration:
                return {"choices": [{"message": {"role": "assistant", "content": "完成"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            return _run_subagent("长任务")

    def test_long_result_summarized_once(self):
        # 第一次回复超长，第二次是压缩后的短结果
        long = "x" * (st._RESULT_MAX + 100)
        short = "压缩后的要点"
        r = self._run([
            {"choices": [{"message": {"role": "assistant", "content": long}}]},
            {"choices": [{"message": {"role": "assistant", "content": short}}]},
        ])
        self.assertEqual(r["result"], short)

    def test_summary_twice_then_truncate_fallback(self):
        # 两次 summary 后仍超长 → 截断兜底
        long1 = "x" * (st._RESULT_MAX + 100)
        long2 = "y" * (st._RESULT_MAX + 100)
        r = self._run([
            {"choices": [{"message": {"role": "assistant", "content": long1}}]},  # 原始超长
            {"choices": [{"message": {"role": "assistant", "content": long2}}]},  # 第一次总结仍超长
            {"choices": [{"message": {"role": "assistant", "content": long2}}]},  # 第二次总结仍超长
        ])
        # 兜底截断到 _RESULT_MAX
        self.assertLessEqual(len(r["result"]), st._RESULT_MAX + 20)
        self.assertIn("已截断", r["result"])

    def test_summary_capped_at_two(self):
        """最多总结 2 次：不会无限循环。"""
        from core import loop as loop_mod

        calls = []
        long = "x" * (st._RESULT_MAX + 100)

        def fake_chat(messages, tools=None):
            calls.append(1)
            return {"choices": [{"message": {"role": "assistant", "content": long}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            r = _run_subagent("长")

        # 原始 1 次 + 最多 2 次总结 = 至多 3 次模型调用
        self.assertLessEqual(len(calls), 3)
        self.assertIn("已截断", r["result"])

    def test_empty_result_on_budget_exhaustion(self):
        """subAgent 预算用尽未产出结果 → 显式 error，而非空 result。"""
        from core import loop as loop_mod

        # 内层模型一直调工具，永不产出最终文本
        calls = [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "x"}'}},
        ]
        idx = [0]

        def fake_chat(messages, tools=None):
            tc = calls[idx[0] % len(calls)]
            idx[0] += 1
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            r = _run_subagent("一直读文件")

        self.assertIn("error", r)
        self.assertIn("未产出结果", r["error"])

    def test_empty_summary_keeps_original_and_truncates(self):
        """summary 返回空时，保留原始超长回复并截断，不丢内容。"""
        from core import loop as loop_mod

        long = "x" * (st._RESULT_MAX + 100)
        seq = [
            {"choices": [{"message": {"role": "assistant", "content": long}}]},  # 原始超长
            {"choices": [{"message": {"role": "assistant", "content": ""}}]},    # summary 返回空
        ]
        idx = [0]

        def fake_chat(messages, tools=None):
            r = seq[min(idx[0], len(seq) - 1)]
            idx[0] += 1
            return r

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            r = _run_subagent("长任务")

        # 结果应保留原始内容截断后的头部（含 x 和 已截断 标记）
        self.assertIn("已截断", r["result"])
        self.assertIn("x", r["result"])
        self.assertLessEqual(len(r["result"]), st._RESULT_MAX + 20)


class TestAgentSessionName(unittest.TestCase):
    """AgentSession 的 name 属性与输出前缀。"""

    def test_default_name_is_agent(self):
        from core.loop import AgentSession
        self.assertEqual(AgentSession().name, "Feidudu")

    def test_custom_name_passed_to_ctx(self):
        from core.loop import AgentSession
        sess = AgentSession(name="sub1")
        self.assertEqual(sess.name, "sub1")
        self.assertEqual(sess.ctx.name, "sub1")

    def test_loop_output_has_name_prefix(self):
        """chat() 的输出（助手/工具调用）应带 [名字] 前缀。"""
        from core import loop as loop_mod
        import io, contextlib

        captured = []

        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            AgentSession(name="sub1").chat("hi", max_steps=2)

        out = buf.getvalue()
        self.assertIn("[sub1]", out)
        self.assertIn("[sub1] 助手：hello", out)


if __name__ == "__main__":
    unittest.main()
