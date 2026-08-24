"""Agent 会话与主循环。

分层：
  - AgentSession：一次会话，跨轮次共享对话历史（messages）与钩子上下文（ctx）。
    chat(user_message) 每调用一次，处理一条用户消息并跑完内部 ReAct 循环。
  - agent_loop：一次性使用的便捷封装（新建会话 → 跑一条消息 → 返回）。

主循环逻辑固定，扩展通过钩子完成（见 core/hooks.py）。

轮数预算（自动续跑）：
  每条消息先分配"单段预算"（AGENT_MAX_STEPS，默认 20 轮）。
  若任务拆了计划且尚未完成、单段预算用尽，则自动续跑：
  注入一条"自动续跑"提醒，继续下一段，直到计划完成或达到总硬上限
  （AGENT_MAX_TOTAL_STEPS 段，默认 3 段；设 0 关闭自动续跑）。
  无活跃计划时，单段预算即硬上限，用尽即停。
"""
import os

from core import planner
from core.hooks import HookContext, HookEvents, hooks
from core.llm import chat_completion
from core.tools import registered_tools, run_tool
from tools import planner_tools

DEFAULT_SYSTEM_PROMPT = "你是一个有用的助手。当需要访问本地环境或执行命令时，请调用可用的工具。"

# 每条用户消息的单段轮数预算（环境变量可调）
DEFAULT_MAX_STEPS = 20
# 自动续跑：总轮数硬上限 = 单段预算 × 续跑段数（设 0 关闭自动续跑）
DEFAULT_MAX_SEGMENTS = 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


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


class AgentSession:
    """一次 Agent 会话：可进行多轮对话，历史与状态跨轮保留。"""

    def __init__(self, system_prompt: str | None = None) -> None:
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
        ]
        self.ctx = HookContext()
        self.ctx.messages = self.messages

    def chat(self, user_message: str, max_steps: int | None = None) -> str:
        """处理一条用户消息，跑完内部 ReAct 循环，返回最终回复文本。

        单段预算默认取环境变量 AGENT_MAX_STEPS（默认 20），传参可覆盖。
        计划未完成时自动续跑，直到计划完成或达到总硬上限
        （AGENT_MAX_TOTAL_STEPS 段，默认 3；设 0 关闭自动续跑）。

        本轮产生的对话历史会保留在 self.messages 中，供后续轮次使用。
        """
        messages = self.messages
        ctx = self.ctx
        messages.append({"role": "user", "content": user_message})

        # 用户输入提交后、首轮请求前
        hooks.fire(HookEvents.USER_PROMPT_SUBMIT, ctx)

        segment = _env_int("AGENT_MAX_STEPS", max_steps or DEFAULT_MAX_STEPS)
        if segment < 1:
            segment = DEFAULT_MAX_STEPS
        max_segments = _env_int("AGENT_MAX_TOTAL_STEPS", DEFAULT_MAX_SEGMENTS)
        auto_continue = max_segments > 0

        # 总轮数硬上限：单段预算 × 段数
        hard_cap = segment * max_segments if auto_continue else segment

        steps_done = 0
        last_answer = ""
        seg_no = 1

        while True:
            print(f"\n=== 执行段 {seg_no}/{max_segments if auto_continue else 1}（单段预算 {segment} 轮）===")
            # 段内循环：跑满单段预算
            for _step in range(1, segment + 1):
                steps_done += 1
                print(f"\n===== 第 {steps_done}/{hard_cap} 轮 =====")
                # 每次调用模型前：钩子可注入上下文（如计划快照提醒，防目标漂移）
                hooks.fire(HookEvents.PRE_MODEL_REQUEST, ctx)
                data = chat_completion(messages, tools=registered_tools())

                message = data["choices"][0]["message"]
                # 记录本轮消耗，便于观察，并累加到共享状态（跨轮累计）
                usage = data.get("usage", {})
                print(f"[tokens] 输入={usage.get('prompt_tokens')} 输出={usage.get('completion_tokens')}")
                s = ctx.state
                s["total_prompt_tokens"] = s.get("total_prompt_tokens", 0) + (usage.get("prompt_tokens") or 0)
                s["total_completion_tokens"] = s.get("total_completion_tokens", 0) + (usage.get("completion_tokens") or 0)

                # 把模型的回复（可能含 tool_calls）加入历史
                messages.append(message)

                # 模型每轮响应后（无论是否调用工具）
                hooks.fire(HookEvents.POST_MODEL_RESPONSE, ctx)

                # 模型没要求调用工具 → 说明答案已经给全，结束本轮
                if not message.get("tool_calls"):
                    answer = message.get("content") or ""
                    print(f"\n助手：{answer}")
                    hooks.fire(HookEvents.STOP, ctx)
                    return answer

                # 模型要求调用工具 → 逐个执行，并以 tool 角色追加进历史
                for tool_call in message["tool_calls"]:
                    tool_name = tool_call["function"]["name"]
                    arguments = tool_call["function"]["arguments"]
                    print(f"调用工具 <{tool_name}>，参数：{arguments}")

                    ctx.tool_name = tool_name
                    ctx.arguments = arguments
                    # 计划工具需要读写 ctx.state：每次执行前绑定当前上下文
                    planner_tools.bind(ctx)

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

            # ---- 单段预算用尽：决定是否自动续跑 ----
            if not auto_continue:
                break  # 自动续跑关闭 → 到此为止
            if steps_done >= hard_cap:
                break  # 已达总硬上限

            # 计划是否存在且未完成 → 值得续跑
            plan = ctx.state.get("plan")
            if not plan or planner.plan_done(plan):
                break

            print(f"\n（第 {seg_no} 段预算用尽，任务计划尚未完成，自动续跑）")
            seg_no += 1
            messages.append({
                "role": "system",
                "content": ("（自动续跑）上段轮数预算已用完，但当前任务计划尚未完成。"
                            "请基于当前计划继续执行剩余 step，直到计划完成。"),
            })

        print(f"\n已达到总轮数上限 {hard_cap}，停止循环。")
        hooks.fire(HookEvents.STOP, ctx)
        return last_answer


def agent_loop(user_message: str, max_steps: int = 20) -> str:
    """一次性使用：新建会话，跑一条消息，返回最终回复。"""
    return AgentSession().chat(user_message, max_steps)
