"""Agent Loop 主循环：逻辑固定，扩展通过钩子完成。

循环结构不再改动，新增功能通过注册钩子（hooks）实现。
"""
from core.hooks import HookContext, HookEvents, hooks
from core.llm import chat_completion
from core.tools import registered_tools, run_tool


def _prompt_user(ctx: HookContext) -> bool:
    """阻塞等待用户输入，决定是否放行一次"需确认"的工具调用。"""
    print(f"\n[权限] 工具 <{ctx.tool_name}> 需要你确认：{ctx.arguments}")
    while True:
        answer = input("      允许执行？(y=允许 / n=拒绝 / 其它=拒绝)：").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("      请输入 y 或 n。")


def agent_loop(user_message: str, max_steps: int = 20) -> None:
    messages: list[dict] = [
        {"role": "system", "content": "你是一个有用的助手。当需要访问本地环境或执行命令时，请调用可用的工具。"},
        {"role": "user", "content": user_message},
    ]
    ctx = HookContext()
    ctx.messages = messages

    # 用户输入提交后、首轮请求前
    hooks.fire(HookEvents.USER_PROMPT_SUBMIT, ctx)

    for step in range(1, max_steps + 1):
        print(f"\n===== 第 {step} 轮 =====")
        data = chat_completion(messages, tools=registered_tools())

        message = data["choices"][0]["message"]
        # 记录本轮消耗，便于观察，并累加到共享状态
        usage = data.get("usage", {})
        print(f"[tokens] 输入={usage.get('prompt_tokens')} 输出={usage.get('completion_tokens')}")
        s = ctx.state
        s["total_prompt_tokens"] = s.get("total_prompt_tokens", 0) + (usage.get("prompt_tokens") or 0)
        s["total_completion_tokens"] = s.get("total_completion_tokens", 0) + (usage.get("completion_tokens") or 0)

        # 把模型的回复（可能含 tool_calls）加入历史
        messages.append(message)

        # 模型每轮响应后（无论是否调用工具）
        hooks.fire(HookEvents.POST_MODEL_RESPONSE, ctx)

        # 模型没要求调用工具 → 说明答案已经给全，结束循环
        if not message.get("tool_calls"):
            print(f"\n助手：{message.get('content')}")
            hooks.fire(HookEvents.STOP, ctx)
            return

        # 模型要求调用工具 → 逐个执行，并以 tool 角色追加进历史
        for tool_call in message["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            arguments = tool_call["function"]["arguments"]
            print(f"调用工具 <{tool_name}>，参数：{arguments}")

            ctx.tool_name = tool_name
            ctx.arguments = arguments

            # 工具执行前：钩子裁决（deny > confirm > allow）
            hooks.fire_pre_tool(ctx)
            verdict = ctx.verdict or "allow"

            if verdict == "confirm":
                if _prompt_user(ctx):
                    result = run_tool(tool_name, arguments)
                else:
                    result = '{"blocked": "用户拒绝了该次操作，请勿重试。"}'
            elif verdict == "deny":
                reason = ctx.deny_reason or "该调用被安全策略拒绝"
                result = f'{{"blocked": "{reason}"}}'
            else:  # allow
                result = run_tool(tool_name, arguments)

            ctx.result = result
            # 工具执行后
            hooks.fire_post_tool(ctx)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],  # 必须与 tool_call.id 对应
                    "content": result,
                }
            )

    print(f"\n已达到最大轮数 {max_steps}，停止循环。")
    hooks.fire(HookEvents.STOP, ctx)
