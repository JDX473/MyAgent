"""Hook 事件系统：把 Loop 的生命周期抽象成可插拔的钩子。

固定的 5 个事件点（Loop 主体不再改动）：
   USER_PROMPT_SUBMIT  用户输入提交后、首轮请求前
   POST_MODEL_RESPONSE 模型每轮响应后（无论是否调用工具）
   PRE_TOOL_EXECUTE    工具执行前（可拦截/询问，见 HookContext.verdict）
   POST_TOOL_EXECUTE   工具执行后（拿到工具返回结果）
   STOP                循环结束时（无论正常返回还是达到上限）

用法：
   hooks.register(HookEvents.PRE_TOOL_EXECUTE, callback, priority=0)
   callback(ctx: HookContext) -> None | "allow" | "confirm" | ("deny", 原因)
   - 返回 None：视为 allow（继续）
   - 返回 "allow"：继续
   - 返回 "confirm"：标记需要用户确认
   - 返回 ("deny", 原因)：拒绝执行，原因会回给模型
   同一事件多个回调按 priority 升序执行；pre 的裁决取最高优先级
   （deny > confirm > allow）。
"""


class HookContext:
    """一次工具调用 / 一个生命周期事件内，钩子间共享的上下文。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []   # 当前对话的完整消息历史
        self.tool_name: str = ""         # 本次要调用的工具名（pre/post tool）
        self.arguments: str = ""         # 模型传来的参数 JSON 字符串（pre/post tool）
        self.result: str = ""            # 工具执行后的 JSON 结果字符串（post tool）
        self.verdict: str | None = None  # pre 钩子的裁决：allow / confirm / deny
        self.deny_reason: str = ""       # deny 时的原因说明
        self.state: dict = {}            # 任意共享状态（跨钩子、跨轮次）

    def deny(self, reason: str) -> tuple[str, str]:
        """便捷方法：返回 ("deny", reason)。"""
        return ("deny", reason)


# 钩子事件名
class HookEvents:
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    POST_MODEL_RESPONSE = "post_model_response"
    PRE_TOOL_EXECUTE = "pre_tool_execute"
    POST_TOOL_EXECUTE = "post_tool_execute"
    STOP = "stop"


class HookRegistry:
    """按事件名注册/触发钩子回调。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[int, callable]]] = {
            HookEvents.USER_PROMPT_SUBMIT: [],
            HookEvents.POST_MODEL_RESPONSE: [],
            HookEvents.PRE_TOOL_EXECUTE: [],
            HookEvents.POST_TOOL_EXECUTE: [],
            HookEvents.STOP: [],
        }

    def register(self, event: str, callback: callable, priority: int = 0) -> None:
        """注册一个钩子。priority 越小越先执行（同事件内）。"""
        if event not in self._handlers:
            raise ValueError(f"未知事件：{event}")
        self._handlers[event].append((priority, callback))
        self._handlers[event].sort(key=lambda p: p[0])

    def _run(self, event: str, ctx: HookContext) -> None:
        """顺序执行某事件的全部回调。"""
        for _, cb in self._handlers[event]:
            print(f"【{event}：{cb.__name__}】")
            cb(ctx)

    def fire(self, event: str, ctx: HookContext) -> None:
        """触发一个非工具事件（user_prompt_submit / post_model_response / stop）。"""
        self._run(event, ctx)

    def fire_pre_tool(self, ctx: HookContext) -> None:
        """触发 pre_tool_execute，聚合所有回调的裁决（deny > confirm > allow）。"""
        ctx.verdict = None
        for _, cb in self._handlers[HookEvents.PRE_TOOL_EXECUTE]:
            print(f"【{HookEvents.PRE_TOOL_EXECUTE}：{cb.__name__}】")
            result = cb(ctx)
            if result is None:
                continue
            if result == "allow":
                if ctx.verdict is None:
                    ctx.verdict = "allow"
            elif result == "confirm":
                ctx.verdict = "confirm"
            elif isinstance(result, tuple) and result[0] == "deny":
                ctx.verdict = "deny"
                ctx.deny_reason = result[1]
                break  # deny 最高优先级，直接短路
        ctx.verdict = ctx.verdict or "allow"

    def fire_post_tool(self, ctx: HookContext) -> None:
        """触发 post_tool_execute。"""
        self._run(HookEvents.POST_TOOL_EXECUTE, ctx)


# 全局单例：所有模块通过 `from core.hooks import hooks` 使用同一个注册表
hooks = HookRegistry()
