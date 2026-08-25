"""聊天式 TUI（chat_tui.py）与统一输出通道（core/output.py）的测试。

用 textual 的 run_test 无头运行 App，验证：
  1. App 启动、输入触发 Agent、输出注入消息区
  2. 权限确认按钮路径：confirm -> 点允许 -> Agent 继续
  3. output.emit：默认 print；set_sink 注入后走 sink；恢复默认
"""
import asyncio
import io
import json
import unittest
from unittest import mock

from core import output
from core import loop as loop_mod


class TestOutputChannel(unittest.TestCase):
    """core/output.py 统一输出通道。"""

    def tearDown(self):
        output.set_sink(None)  # 恢复默认

    def test_default_prints(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
            output.emit("hello")
        self.assertIn("hello", buf.getvalue())

    def test_set_sink_routes_output(self):
        captured = []
        output.set_sink(lambda text: captured.append(text))
        output.emit("line1")
        output.emit("line2")
        self.assertEqual(captured, ["line1", "line2"])

    def test_sink_error_falls_back_to_print(self):
        """sink 抛异常时退回默认 print，不崩溃。"""
        def bad_sink(text):
            raise RuntimeError("boom")
        output.set_sink(bad_sink)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
            output.emit("fallback")  # 不应抛异常
        self.assertIn("fallback", buf.getvalue())

    def test_is_default(self):
        self.assertTrue(output.is_default())
        output.set_sink(lambda t: None)
        self.assertFalse(output.is_default())
        output.set_sink(None)
        self.assertTrue(output.is_default())


class TestChatTui(unittest.TestCase):
    """聊天式 TUI 的无头运行测试。"""

    def _run_app(self, send_text, mock_chat):
        """用 run_test 无头跑 App，返回消息区 lines。"""
        with mock.patch.object(loop_mod, "chat_completion", side_effect=mock_chat):
            from chat_tui import ChatApp

            async def go():
                app = ChatApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    inp = app.query_one("#input")
                    inp.focus()
                    inp.value = send_text
                    await pilot.press("enter")
                    # 等 worker 线程跑完
                    for _ in range(100):
                        await pilot.pause()
                        await asyncio.sleep(0.03)
                        if not app._busy:
                            break
                    log = app.query_one("#messages")
                    lines = [str(l) for l in getattr(log, "lines", [])]
                    return lines
            return asyncio.run(go())

    def test_chat_appends_reply_to_messages(self):
        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "你好！我是 Agent。"}}]}

        lines = self._run_app("你好", fake_chat)
        joined = "\n".join(lines)
        self.assertIn("你好！我是 Agent", joined)   # Agent 回复
        self.assertIn("你好", joined)              # 用户消息

    def test_confirm_button_path(self):
        """模型调 git push（confirm）→ 弹按钮 → 点允许 → Agent 继续。"""
        calls = [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": '{"command": "git push origin main"}'}},
            None,
        ]
        idx = [0]

        def fake_chat(messages, tools=None):
            i = idx[0]; idx[0] += 1
            tc = calls[i] if i < len(calls) else None
            if tc is None:
                return {"choices": [{"message": {"role": "assistant", "content": "push 完成"}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            from chat_tui import ChatApp

            async def go():
                app = ChatApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    inp = app.query_one("#input")
                    inp.focus(); inp.value = "推送一下"; await pilot.press("enter")
                    # 等 confirm 出现
                    for _ in range(100):
                        await pilot.pause(); await asyncio.sleep(0.03)
                        if app._pending_confirm:
                            break
                    self.assertTrue(app._pending_confirm, "confirm 未弹出")
                    req_id = app._pending_confirm[0][0]
                    btn = app.query_one(f"#allow-{req_id}")
                    await pilot.click(btn)
                    for _ in range(120):
                        await pilot.pause(); await asyncio.sleep(0.03)
                        if not app._busy:
                            break
                    log = app.query_one("#messages")
                    lines = [str(l) for l in getattr(log, "lines", [])]
                    return lines
            lines = asyncio.run(go())

        joined = "\n".join(lines)
        self.assertIn("push 完成", joined)   # 允许后 Agent 继续
        self.assertIn("允许", joined)        # 确认结果被记录

if __name__ == "__main__":
    unittest.main()
