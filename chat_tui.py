"""聊天式 TUI：类似 Claude Code 的全屏交互界面。

布局：
  - 上方滚动消息区：用户 / Agent / 工具调用 / subAgent 委派 分角色着色
  - 底部输入框：回车发送，Ctrl+C 或输入 /exit 退出
  - 工具需要权限确认时，输入框上方弹出确认行（Y=允许 / N=拒绝）

实现：
  - 用 textual（第三方库，需 pip install textual）。
  - AgentSession 在 worker 线程跑；core.output.set_sink 把 Agent 的控制台
    输出逐行注入 TUI 消息区；确认通过 set_confirm_handler 注入。
  - 终端模式（python main.py）完全不受影响。

运行：
  python main.py --tui
"""
import threading

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Footer, Input, Label, RichLog

from core import output
from core.loop import AgentSession, set_confirm_handler


class ChatApp(App):
    """全屏聊天式 Agent 界面。"""

    TITLE = "Feidudu"
    CSS = """
    Screen {
        layout: vertical;
    }
    #messages {
        width: 100%;
        height: 1fr;
        border: round #2d3748;
        padding: 0 1;
        overflow-y: scroll;
    }
    #confirm {
        height: auto;
        display: none;
        padding: 0 1;
    }
    #confirm.visible {
        display: block;
    }
    #confirm Label {
        color: #facc15;
        margin-bottom: 2;
    }
    #confirm Button {
        margin-right: 2;
    }
    #input {
        dock: bottom;
        width: 100%;
    }
    """

    def __init__(self, session: AgentSession | None = None) -> None:
        super().__init__()
        self.session = session or AgentSession()
        self._worker: threading.Thread | None = None
        self._busy = False
        self._pending_confirm: list = []  # 待用户裁决的确认

    # ---- 生命周期 ----
    def on_mount(self) -> None:
        output.set_sink(self._on_agent_output)
        set_confirm_handler(self._on_confirm)
        log = self.query_one("#messages", RichLog)
        # 启动 logo：优先 res/feidudu.png 图片版（Pillow），回退 ASCII banner
        try:
            from core.logo import logo_ansi
            from rich.text import Text
            ansi = logo_ansi(width=60, max_height=28)
            if ansi:
                log.write(Text.from_ansi(ansi))
            else:
                from core.banner import banner
                log.write(banner())
        except Exception:
            from core.banner import banner
            log.write(banner())
        log.write("Feidudu 已启动。输入问题开始对话（/exit 退出）。")

    def on_unmount(self) -> None:
        output.set_sink(None)
        set_confirm_handler(None)

    # ---- UI 构建 ----
    def compose(self) -> ComposeResult:
        yield RichLog(id="messages", wrap=True, highlight=True)
        yield Vertical(id="confirm")
        yield Input(placeholder="输入问题，回车发送…", id="input")
        yield Footer()

    # ---- 消息区智能滚动：在底部才自动跟随，向上翻历史时不打扰 ----
    def _on_agent_output(self, text: str) -> None:
        """worker 线程 → UI 线程：把一行 Agent 输出追加到消息区。"""
        self.call_from_thread(self._append_output, text)

    def _append_output(self, text: str) -> None:
        log = self.query_one("#messages", RichLog)
        # 记录当前是否在底部（在底部才跟随新消息自动滚动）
        at_bottom = log.is_vertical_scroll_end
        # 显式控制本次写入是否滚到底：在底部才跟随，翻历史时不 yank
        log.write(text, scroll_end=at_bottom)

    # ---- 权限确认（注入 confirm handler）----
    def _on_confirm(self, tool: str, arguments: str) -> bool:
        """worker 线程调用：把确认请求交给 UI，阻塞等用户裁决。

        通过 threading.Event 阻塞 worker 线程，UI 线程点按钮或输入 Y/N 后解锁。
        """
        import threading as _t
        import uuid

        req_id = uuid.uuid4().hex
        evt = _t.Event()
        result = {"allowed": None}
        self._pending_confirm.append((req_id, evt, result))
        self.call_from_thread(self._show_confirm, req_id, tool, arguments)
        evt.wait(timeout=600)  # 超时按拒绝
        if result.get("allowed") is None:
            # 超时未裁决：清理残留条目与确认框，避免幽灵条目影响后续裁决
            self._remove_pending(req_id, timed_out=True)
            return False
        return bool(result.get("allowed"))

    def _remove_pending(self, req_id: str, timed_out: bool = False) -> None:
        """从待确认列表移除条目；若无剩余则收起确认框（用于超时清理）。"""
        for i, (rid, _evt, _res) in enumerate(self._pending_confirm):
            if rid == req_id:
                self._pending_confirm.pop(i)
                break
        try:
            # 在 UI 线程收起确认框；App 关闭时可能抛 RuntimeError，忽略
            self.call_from_thread(self._dismiss_confirm)
        except RuntimeError:
            pass
        if timed_out:
            try:
                self.call_from_thread(self._append_output, "[权限] 确认超时，已按拒绝处理")
            except RuntimeError:
                pass

    def _show_confirm(self, req_id: str, tool: str, arguments: str) -> None:
        # 只渲染列表第一个（前端）确认项：界面显示的必须与输入裁决的目标一致。
        # 若传入的 req_id 不是第一个（说明已有更新的确认顶上），跳过本次渲染。
        if not self._pending_confirm or self._pending_confirm[0][0] != req_id:
            return
        box = self.query_one("#confirm", Vertical)
        box.styles.display = "block"
        box.add_class("visible")
        box.remove_children()
        box.mount(Label(f"工具 <{tool}> 需要确认：{arguments}  (输入 y 允许 / n 拒绝，或点击按钮)"))
        y = Button("允许 (Y)", id=f"allow-{req_id}", variant="success")
        n = Button("拒绝 (N)", id=f"deny-{req_id}", variant="error")
        box.mount(y)
        box.mount(n)
        # 确认框弹出时，让输入框保持/获得焦点，方便直接输 y/n
        self.query_one("#input", Input).focus()

    def _dismiss_confirm(self) -> None:
        box = self.query_one("#confirm", Vertical)
        box.styles.display = "none"
        box.remove_class("visible")
        box.remove_children()

    def _resolve_confirm(self, req_id: str, allowed: bool) -> None:
        for i, (rid, evt, result) in enumerate(self._pending_confirm):
            if rid == req_id:
                result["allowed"] = allowed
                evt.set()
                self._pending_confirm.pop(i)
                self._append_output(f"[权限] 你选择了 {'允许' if allowed else '拒绝'}")
                # 若有下一个待确认项，渲染它；否则收起确认框
                if self._pending_confirm:
                    nxt = self._pending_confirm[0]
                    self._show_confirm(nxt[0], nxt[1], nxt[2])
                else:
                    self._dismiss_confirm()
                # 把焦点还给输入框（点击按钮后焦点会停在已移除的按钮上）
                self.query_one("#input", Input).focus()
                return

    @on(Button.Pressed)
    def _on_confirm_button(self, event) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("allow-"):
            self._resolve_confirm(btn_id[len("allow-"):], True)
        elif btn_id.startswith("deny-"):
            self._resolve_confirm(btn_id[len("deny-"):], False)

    # ---- 发送 / 退出 ----
    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            self.query_one("#input", Input).value = ""
            return
        if text in ("/exit", "/quit"):
            self.query_one("#input", Input).value = ""
            self.exit()
            return
        if text == "/clear":
            self.query_one("#input", Input).value = ""
            self.query_one("#messages", RichLog).clear()
            return
        # 权限确认期间：输入 y / n 直接裁决
        if self._pending_confirm:
            low = text.lower()
            req_id = self._pending_confirm[0][0]
            self.query_one("#input", Input).value = ""
            if low in ("y", "yes", "允许", "1"):
                self._resolve_confirm(req_id, True)
            elif low in ("n", "no", "拒绝", "0"):
                self._resolve_confirm(req_id, False)
            else:
                self._append_output("[权限] 请输入 y（允许）或 n（拒绝），或点击上方按钮")
            return
        if self._busy:
            self.query_one("#input", Input).value = ""
            self._append_output("[系统] Agent 正在处理，请稍候…")
            return
        # 正常发送：清空输入并交给 Agent
        self.query_one("#input", Input).value = ""
        self._append_output(f"[你] {text}")
        self._busy = True
        # worker 线程跑 Agent
        self._worker = threading.Thread(target=self._run_agent, args=(text,), daemon=True)
        self._worker.start()

    def _run_agent(self, text: str) -> None:
        """后台线程：跑 AgentSession.chat()，输出经 sink 注入 UI。"""
        try:
            self.session.chat(text)
        except Exception as e:
            self.call_from_thread(self._append_output, f"[系统] 出错了：{e}")
        finally:
            self.call_from_thread(self._set_idle)

    def _set_idle(self) -> None:
        self._busy = False
        self._append_output("")


def run_tui() -> None:
    """启动聊天式 TUI（供 main.py --tui 调用）。"""
    ChatApp().run()


if __name__ == "__main__":
    run_tui()
