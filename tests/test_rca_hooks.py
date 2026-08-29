"""RCA 钩子(rcagent/rca_hooks.py)+ 触发它们的 loop 行为测试。

覆盖:
  1. dup_call_detector:相同参数重复调用 → 注入提醒;非 RCA 会话不触发(隔离)
  2. premature_finalize_guard:调查不足(采集 < 2)打回 finalize;充足放行;
     acknowledge 显式放行
  3. finalize_done:成功 finalize → 保存报告并置 _agent_finished
  4. loop 集成(finalize 主动终结 / 过早 finalize 被拦 / 非 RCA 会话不终结)
"""
import json
import unittest
from unittest import mock

import rcagent  # noqa: F401  触发钩子注册 + JSON 修复(副作用)
from core import loop as loop_mod
from core.hooks import HookContext
from core.tools import run_tool
from rcagent.rca_hooks import (
    dup_call_detector,
    finalize_done,
    premature_finalize_guard,
)

_FINALIZE_ARGS = {
    "root_cause": "ES 连接超时",
    "solution": "联系 ES 团队",
    "evidence": "SocketTimeoutException",
    "responsibility": "平台",
}

_SURVEY_CALL = ("query_logs", '{"entity_id": "job.log", "keyword": ""}')


def _finalize_result(**kw):
    args = dict(_FINALIZE_ARGS)
    args.update(kw)
    return run_tool("finalize", json.dumps(args, ensure_ascii=False))


def _mk_ctx(name="RCA", tool="", result="", arguments="", tool_history=None, messages=None):
    ctx = HookContext()
    ctx.name = name
    ctx.tool_name = tool
    ctx.result = result
    ctx.arguments = arguments
    if tool_history:
        ctx.state["tool_history"] = tool_history
    if messages:
        ctx.messages = messages
    else:
        ctx.messages = []
    return ctx


class TestDupCallDetector(unittest.TestCase):

    def test_same_args_twice_rewrites_result(self):
        ctx = _mk_ctx(tool="query_logs", arguments='{"entity_id": "x"}', result="ok")
        dup_call_detector(ctx)
        self.assertEqual(len(ctx.state["tool_history"]), 1)
        dup_call_detector(ctx)
        # 第二次重复 → 改写结果,而不是往 messages 插 system(后者会打断 API 配对)
        self.assertIn("重复无效调用", ctx.result)
        self.assertEqual(len(ctx.messages), 0)

    def test_different_args_no_rewrite(self):
        ctx = _mk_ctx(tool="query_logs", arguments='{"entity_id": "a"}', result="r1")
        dup_call_detector(ctx)
        ctx.arguments = '{"entity_id": "b"}'
        dup_call_detector(ctx)
        self.assertEqual(len(ctx.state["tool_history"]), 2)
        self.assertEqual(ctx.result, "r1")
        self.assertEqual(len(ctx.messages), 0)

    def test_non_rca_session_ignored(self):
        ctx = _mk_ctx(name="Agent", tool="query_logs", arguments='{"entity_id": "x"}')
        dup_call_detector(ctx)
        dup_call_detector(ctx)
        self.assertNotIn("tool_history", ctx.state)


class TestPrematureFinalizeGuard(unittest.TestCase):

    def test_blocked_when_insufficient_survey(self):
        ctx = _mk_ctx(tool="finalize", result=_finalize_result(),
                      tool_history=[_SURVEY_CALL])
        premature_finalize_guard(ctx)
        self.assertIn("拦截", ctx.result)
        self.assertNotIn("root_cause", ctx.result)

    def test_passed_when_enough_survey(self):
        before = _finalize_result()
        ctx = _mk_ctx(tool="finalize", result=before,
                      tool_history=[_SURVEY_CALL, _SURVEY_CALL])
        premature_finalize_guard(ctx)
        self.assertEqual(ctx.result, before)

    def test_acknowledge_allows_through(self):
        res = _finalize_result(acknowledge=True)
        ctx = _mk_ctx(tool="finalize", result=res, tool_history=[_SURVEY_CALL])
        premature_finalize_guard(ctx)
        self.assertEqual(ctx.result, res)
        self.assertTrue(ctx.state.get("rca_acknowledged"))

    def test_non_rca_ignored(self):
        before = _finalize_result()
        ctx = _mk_ctx(name="Agent", tool="finalize", result=before,
                      tool_history=[_SURVEY_CALL])
        premature_finalize_guard(ctx)
        self.assertEqual(ctx.result, before)


class TestFinalizeDone(unittest.TestCase):

    def test_sets_agent_finished_and_report(self):
        ctx = _mk_ctx(tool="finalize", result=_finalize_result())
        finalize_done(ctx)
        self.assertTrue(ctx.state.get("_agent_finished"))
        self.assertIn("根因", ctx.state["rca_report"])
        self.assertEqual(ctx.state["rca_report_raw"]["responsibility"], "platform")

    def test_blocked_result_does_not_finish(self):
        ctx = _mk_ctx(tool="finalize", result='{"error": "被拦截"}')
        finalize_done(ctx)
        self.assertNotIn("_agent_finished", ctx.state)

    def test_non_rca_ignored(self):
        ctx = _mk_ctx(name="Agent", tool="finalize", result=_finalize_result())
        finalize_done(ctx)
        self.assertNotIn("_agent_finished", ctx.state)


def _tool_call(name, args, cid="c1"):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class TestFinalizeLoopIntegration(unittest.TestCase):
    """mock chat_completion:finalize 让 loop 主动终结;过早 finalize 被拦。"""

    def _run(self, script, name="RCA"):
        seen = []
        idx = [0]

        def fake_chat(messages, tools=None):
            seen.append([dict(m) for m in messages])
            i = idx[0]
            idx[0] += 1
            tc = script[i] if i < len(script) else None
            if tc is None:
                return {"choices": [{"message": {"role": "assistant", "content": "调查结束"}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            sess = loop_mod.AgentSession(name=name)
            answer = sess.chat("找出 job-x 失败根因", max_steps=10)
        return answer, seen, sess

    def test_finalize_terminates_loop_with_report(self):
        script = [
            _tool_call("list_entities", {"filter_text": ""}, cid="l"),
            _tool_call("query_logs", {"entity_id": "job-x.log", "keyword": "FATAL"}, cid="q"),
            _tool_call("finalize", dict(_FINALIZE_ARGS, responsibility="平台"), cid="f"),
        ]
        answer, seen, sess = self._run(script)
        self.assertTrue(sess.ctx.state.get("_agent_finished"))
        self.assertIn("根因", answer)
        self.assertIn("责任归属", answer)
        self.assertEqual(len(seen), 3)  # 没有再请求第 4 次 → 真正终结

    def test_early_finalize_blocked_and_loop_continues(self):
        script = [
            _tool_call("query_logs", {"entity_id": "job-x.log"}, cid="q"),   # 调查 1 次,不足
            _tool_call("finalize", dict(_FINALIZE_ARGS, responsibility="平台"), cid="f"),
        ]
        answer, seen, sess = self._run(script)
        self.assertNotIn("_agent_finished", sess.ctx.state)
        self.assertEqual(answer, "调查结束")  # 走正常文本出口
        # 模型应看到过"被系统拦截"的字样
        all_tool = " ".join(
            m.get("content", "") for msgs in seen for m in msgs if m.get("role") == "tool")
        self.assertIn("拦截", all_tool)

    def test_non_rca_session_finalize_does_not_terminate(self):
        script = [
            _tool_call("query_logs", {"entity_id": "job-x.log"}, cid="q"),
            _tool_call("query_logs", {"entity_id": "job-x.log"}, cid="q2"),
            _tool_call("finalize", dict(_FINALIZE_ARGS, responsibility="平台"), cid="f"),
        ]
        answer, seen, sess = self._run(script, name="Agent")
        self.assertNotIn("_agent_finished", sess.ctx.state)
        self.assertEqual(answer, "调查结束")


def _assert_tool_pairing(msgs: list[dict]) -> None:
    """对每个含 tool_calls 的 assistant 消息,紧随其后必须有等量 tool 回应,期间不得夹 system。

    这条不变量一旦被破坏,DeepSeek API 会返回 400(insufficient tool messages),
    是 RCA 钩子(如重复调用检测)绝不能违反的红线。
    """
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            n = len(m["tool_calls"])
            for k in range(n):
                assert msgs[i + 1 + k].get("role") == "tool", \
                    f"tool_calls 之后第 {k} 条不是 tool 回应: {msgs[i+1+k].get('role')!r}"
                assert "tool_call_id" in msgs[i + 1 + k]
            assert all(mm.get("role") != "system" for mm in msgs[i + 1: i + 1 + n]), \
                "tool_calls 与 tool 回应之间夹了 system 消息"
            i += 1 + n
        else:
            i += 1


class TestToolPairingInvariant(unittest.TestCase):
    """同一消息里多个 tool_calls:重复调用被改写,但配对不变量必须保持(回归 400 bug)。"""

    def test_same_args_twice_in_one_message_keeps_pairing(self):
        from core import loop as loop_mod

        tc1 = _tool_call("query_logs", {"entity_id": "job.log"}, cid="q1")
        tc2 = _tool_call("query_logs", {"entity_id": "job.log"}, cid="q2")  # 与 tc1 相同参数
        seen = []
        idx = [0]

        def fake_chat(messages, tools=None):
            seen.append([dict(m) for m in messages])
            if idx[0] == 0:
                idx[0] += 1
                return {"choices": [{"message": {"role": "assistant", "content": "",
                                                 "tool_calls": [tc1, tc2]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "调查完成"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            sess = loop_mod.AgentSession(name="RCA")
            ans = sess.chat("排查", max_steps=5)

        self.assertEqual(ans, "调查完成")
        last_msgs = seen[-1]
        _assert_tool_pairing(last_msgs)
        # 第二条重复调用被改写为"重复无效调用"错误
        tool_results = [m.get("content", "") for m in last_msgs if m.get("role") == "tool"]
        self.assertTrue(any("重复无效调用" in c for c in tool_results))


if __name__ == "__main__":
    unittest.main()