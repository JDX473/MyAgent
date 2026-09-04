#!/usr/bin/env python3
"""Agent —— 基于 DeepSeek 原生 REST API 的最简 ReAct 循环。

入口：先 import config（加载 .env），再 import tools 包（注册工具 + 默认钩子），
然后启动交互式对话。支持多轮连续对话（跨轮保留历史）。

运行前提：
  设置环境变量 DEEPSEEK_API_KEY（在 https://platform.deepseek.com 申请），
  或在本目录的 .env 文件中配置（两者任一即可，已设置的环境变量优先）。
  可选 DEEPSEEK_MODEL，默认 deepseek-v4-flash。
  仅用 Python 标准库，无需 pip install 任何东西。

用法：
  python main.py
"""
import config  # noqa: F401  触发 .env 加载与配置读取
from core.loop import AgentSession
from tools import (  # noqa: F401  触发工具注册 + 默认钩子注册（副作用）
    bash,
    edit,
    get_environment,
    glob,
    read,
    read_code,
    search_code,
    search_logs,
    search_symbol,
    websearch,
    write,
)

from config import BOCHA_API_KEY, DEEPSEEK_API_KEY

HELP_TEXT = """可用的交互命令（输入以 / 开头）：
  /exit 或 /quit   退出
  /clear           清空当前会话历史，重新开始
  /help            显示本帮助"""


def main() -> None:
    if not DEEPSEEK_API_KEY:
        print("未检测到 DEEPSEEK_API_KEY。可通过以下任一方式配置：")
        print("1. 设置环境变量，例如（PowerShell）：")
        print('      $env:DEEPSEEK_API_KEY = "你的key"')
        print("2. 在本项目目录创建 .env 文件（推荐，已被 git 忽略）：")
        print('      DEEPSEEK_API_KEY=你的key')
        print("   可选在 .env 中配置：DEEPSEEK_MODEL=deepseek-v4-pro")
        print("   websearch 工具需要：BOCHA_API_KEY=你的博查key（https://open.bocha.cn）")
        raise SystemExit(1)

    print("Agent 已启动，输入 /help 查看命令，输入 /exit 退出。\n")
    session = AgentSession()

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
                # 新建会话，丢弃历史（保留当前钩子注册表）
                session = AgentSession()
                print("[会话已清空]")
                continue
            if cmd == "/help":
                print(HELP_TEXT)
                continue
            print(f"未知命令：{user_input}（输入 /help 查看可用命令）")
            continue

        session.chat(user_input)


if __name__ == "__main__":
    main()
