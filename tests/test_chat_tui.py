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

    def test_scroll_preserved_when_reading_history(self):
        """向上翻历史时，新内容到达不应把用户拉回底部。"""
        from chat_tui import ChatApp

        def fake_chat(messages, tools=None):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch.object(loop_mod, "chat_completion", side_effect=fake_chat):
            async def go():
                app = ChatApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    log = app.query_one("#messages")
                    # 写入足够多行制造滚动
                    for i in range(60):
                        log.write(f"line {i} " + "x" * 50)
                    await pilot.pause()
                    # 向上翻两行
                    log.scroll_up()
                    log.scroll_up()
                    await pilot.pause()
                    y_before = log.scroll_offset.y
                    self.assertFalse(log.is_vertical_scroll_end, "翻后不应在底部")
                    # 走正式路径写入新内容
                    app._append_output("NEW CONTENT")
                    await pilot.pause()
                    # 不应被拉回底部
                    self.assertEqual(log.scroll_offset.y, y_before, "翻历史后新内容被拉回底部")
            asyncio.run(go())

    def _run_confirm(self, respond_fn):
        """通用：触发 confirm → 用 respond_fn(btn_id/pending) 裁决 → 返回消息区。"""
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
                    await respond_fn(app, pilot, inp)
                    for _ in range(120):
                        await pilot.pause(); await asyncio.sleep(0.03)
                        if not app._busy:
                            break
                    log = app.query_one("#messages")
                    lines = [str(l) for l in getattr(log, "lines", [])]
                    return lines
            return asyncio.run(go())

    def test_confirm_button_path(self):
        """模型调 git push（confirm）→ 弹按钮 → 点允许 → Agent 继续。"""
        async def click_allow(app, pilot, inp):
            req_id = app._pending_confirm[0][0]
            btn = app.query_one(f"#allow-{req_id}")
            await pilot.click(btn)

        joined = "\n".join(self._run_confirm(click_allow))
        self.assertIn("push 完成", joined)   # 允许后 Agent 继续
        self.assertIn("允许", joined)        # 确认结果被记录

    def test_confirm_keyboard_y(self):
        """confirm 期间在输入框输 y 回车 → 允许 → Agent 继续。"""
        async def type_y(app, pilot, inp):
            inp.value = "y"
            await pilot.press("enter")

        joined = "\n".join(self._run_confirm(type_y))
        self.assertIn("push 完成", joined)   # 允许后 Agent 继续
        self.assertIn("允许", joined)        # 确认结果被记录

    def test_confirm_keyboard_n_denies(self):
        """confirm 期间输 n 回车 → 拒绝 → 工具被 blocked。"""
        async def type_n(app, pilot, inp):
            inp.value = "n"
            await pilot.press("enter")

        joined = "\n".join(self._run_confirm(type_n))
        self.assertIn("拒绝", joined)        # 确认结果记录为拒绝

    def test_confirm_click_returns_focus_and_input_works(self):
        """点击允许后，焦点回到输入框，且能继续输入。"""
        async def click_allow_then_type(app, pilot, inp):
            req_id = app._pending_confirm[0][0]
            btn = app.query_one(f"#allow-{req_id}")
            await pilot.click(btn)
            # 等裁决完成（worker 继续）
            for _ in range(120):
                await pilot.pause(); await asyncio.sleep(0.03)
                if not app._busy:
                    break
            # 焦点应回到输入框
            self.assertIs(app.focused, inp, f"焦点未回输入框: {app.focused}")
            # 输入框可继续输入
            inp.value = "继续对话"
            await pilot.press("enter")

        joined = "\n".join(self._run_confirm(click_allow_then_type))
        self.assertIn("继续对话", joined)    # 点击后输入框能输入并提交

    def test_nested_confirm_orders_fifo(self):
        """多个待确认项时：界面显示第一个，输入 y/n 裁决第一个，然后自动显示下一个。"""
        from textual.widgets import Button
        from chat_tui import ChatApp
        import threading as _t
        import uuid

        with mock.patch.object(loop_mod, "chat_completion"):
            async def go():
                app = ChatApp()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    reqA = uuid.uuid4().hex
                    reqB = uuid.uuid4().hex
                    app._pending_confirm.append((reqA, _t.Event(), {"allowed": None}))
                    app._pending_confirm.append((reqB, _t.Event(), {"allowed": None}))
                    # 渲染第一个 A
                    app._show_confirm(reqA, "bash", '{"command":"git push"}')
                    await pilot.pause()
                    btns = [b.id for b in app.query_one("#confirm").query(Button)]
                    self.assertIn(f"allow-{reqA}", btns, "界面未显示第一个确认")
                    # 输入 y 裁决第一个
                    inp = app.query_one("#input")
                    inp.focus(); inp.value = "y"; await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(app._pending_confirm), 1)
                    self.assertEqual(app._pending_confirm[0][0], reqB, "裁决的不是第一个")
                    # 自动显示下一个 B
                    btns2 = [b.id for b in app.query_one("#confirm").query(Button)]
                    self.assertIn(f"allow-{reqB}", btns2, "未自动显示下一个")
                    # 裁决 B，确认框收起
                    inp.value = "n"; await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(app._pending_confirm), 0)
                    btns3 = [b.id for b in app.query_one("#confirm").query(Button)]
                    self.assertEqual(len(btns3), 0, "确认框未收起")
            asyncio.run(go())

if __name__ == "__main__":
    unittest.main()
