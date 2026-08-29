#!/usr/bin/env python3
"""RCA 根因定位 Agent 入口（垂直应用）。

用法：
  python main_rca.py

与 main.py（通用 Agent）的区别：
  - 系统提示词换成 RCA 领域提示（四要素输出规范 + 责任归属规则）；
  - 工具集收窄为"只读 RCA"集：采集/分析/快照/报告，不含 bash/write/edit/计划，
    防止模型在诊断过程中越权写改；
  - 挂载 RCA 专有钩子：OBSK 快照压缩、重复调用检测、过早 finalize 打回、
    finalize 主动终结。

数据源：默认 RCA_ADAPTER=demo（读 ./rca_demo_data 目录下的日志文件），
设 RCA_DEMO_DATA_DIR 可指向其它目录；接入真实系统见 rcagent/adapters/base.py。
"""
import config  # noqa: F401  触发 .env 加载与配置读取
import sys

import rcagent  # noqa: F401  注册 rca 工具 + 钩子（副作用）
from config import DEEPSEEK_API_KEY
from core.loop import AgentSession
from rcagent.rca_prompts import RCA_SYSTEM_PROMPT
from tools import (  # noqa: F401  通用工具注册（含默认权限钩子，副作用）
    get_environment,
    get_plan,
    glob,
    plan_task,
    read,
    revise_plan,
    subagent,
    update_step,
    websearch,
)

# RCA 会话允许的工具集：只读 RCA 工具 + 少量只读通用工具 + subagent/websearch。
# 刻意排除 bash / write / edit / glob —— 诊断过程只读，处置留给人工。
RCA_ALLOWED_TOOLS = {
    "list_entities", "query_logs", "get_entity_detail",
    "analyze_logs", "get_snapshot", "finalize",
    "read", "get_environment", "websearch", "subagent",
}

HELP_TEXT = """可用的交互命令（输入以 / 开头）：
  /exit 或 /quit   退出
  /clear           清空当前会话历史，重新开始
  /tools           列出本会话可用工具
  /help            显示本帮助"""


def _print_setup() -> None:
    print("RCA 根因定位 Agent 已启动（只读诊断 + 结构化报告，处置由人工完成）\n")
    a = rcagent.adapters.get_adapter()
    target = getattr(a, "dir", None) or getattr(a, "base", None) or "—"
    print(f"当前数据源: {a.name} ({target})")
    print("切换数据源: 设环境变量 RCA_ADAPTER=demo 或 im-logsearch")
    print("  - demo:         本地日志目录(默认 ./rca_demo_data,不接真实系统)")
    print("  - im-logsearch: QuantumLink IM 日志查询平台"
          "(默认 http://127.0.0.1:8083,需先启动该服务)")
    print()


def main() -> None:
    if not DEEPSEEK_API_KEY:
        print("未检测到 DEEPSEEK_API_KEY。可通过以下任一方式配置：")
        print("1. 设置环境变量，例如（PowerShell）：")
        print('      $env:DEEPSEEK_API_KEY = "你的key"')
        print("2. 在本项目目录创建 .env 文件（推荐，已被 git 忽略）：")
        print('      DEEPSEEK_API_KEY=你的key')
        print("   可选：RCA_DEMO_DATA_DIR=日志目录（不设则用 ./rca_demo_data）")
        sys.exit(1)

    try:
        _print_setup()
    except RuntimeError as e:
        print(f"数据源初始化失败：{e}")
        sys.exit(1)

    session = AgentSession(system_prompt=RCA_SYSTEM_PROMPT, name="RCA")

    while True:
        try:
            user_input = input("\n你：> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd = user_input.lower()
            if cmd in ("/exit", "/quit"):
                print("再见。")
                break
            if cmd == "/clear":
                session = AgentSession(system_prompt=RCA_SYSTEM_PROMPT, name="RCA")
                print("[会话已清空]")
                continue
            if cmd == "/tools":
                print("可用工具：" + "、".join(sorted(RCA_ALLOWED_TOOLS)))
                continue
            if cmd == "/help":
                print(HELP_TEXT)
                continue
            print(f"未知命令：{user_input}（输入 /help 查看可用命令）")
            continue

        session.chat(user_input, allowed_tools=RCA_ALLOWED_TOOLS)


if __name__ == "__main__":
    main()