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

    # ---- Agent 输出注入 ----
    def _on_agent_output(self, text: str) -> None:
        """worker 线程 → UI 线程：把一行 Agent 输出追加到消息区。"""
        self.call_from_thread(self._append_output, text)

    def _append_output(self, text: str) -> None:
        self.query_one("#messages", RichLog).write(text)

    # ---- 权限确认（注入 confirm handler）----
    def _on_confirm(self, tool: str, arguments: str) -> bool:
        """worker 线程调用：把确认请求交给 UI，阻塞等用户裁决。

        通过 threading.Event 阻塞 worker 线程，UI 线程点按钮后解锁。
        """
        import threading as _t
        import uuid

        req_id = uuid.uuid4().hex
        evt = _t.Event()
        result = {"allowed": None}
        self._pending_confirm.append((req_id, evt, result))
        self.call_from_thread(self._show_confirm, req_id, tool, arguments)
        evt.wait(timeout=600)  # 超时按拒绝
        return bool(result.get("allowed"))

    def _show_confirm(self, req_id: str, tool: str, arguments: str) -> None:
        box = self.query_one("#confirm", Vertical)
        box.styles.display = "block"
        box.remove_children()
        box.mount(Label(f"工具 <{tool}> 需要确认：{arguments}"))
        y = Button("允许 (Y)", id=f"allow-{req_id}", variant="success")
        n = Button("拒绝 (N)", id=f"deny-{req_id}", variant="error")
        box.mount(y)
        box.mount(n)

    @on(Button.Pressed)
    def _on_confirm_button(self, event) -> None:
        btn_id = event.button.id or ""
        # 找出对应的待确认项
        for i, (req_id, evt, result) in enumerate(self._pending_confirm):
            if f"allow-{req_id}" == btn_id or f"deny-{req_id}" == btn_id:
                result["allowed"] = f"allow-{req_id}" == btn_id
                evt.set()
                self._pending_confirm.pop(i)
                # 收起确认行
                box = self.query_one("#confirm", Vertical)
                box.styles.display = "none"
                box.remove_children()
                self._append_output(f"[权限] 你选择了 {'允许' if result['allowed'] else '拒绝'}")
                return

    # ---- 发送 / 退出 ----
    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        if text in ("/exit", "/quit"):
            self.exit()
            return
        if text == "/clear":
            self.query_one("#messages", RichLog).clear()
            return
        if self._busy:
            self._append_output("[系统] Agent 正在处理，请稍候…")
            return
        # 显示用户消息
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
