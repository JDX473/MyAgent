#!/usr/bin/env python3
"""Agent Loop —— 基于 DeepSeek 原生 REST API 的最简 ReAct 循环。

入口：先 import config（加载 .env），再 import tools 包（注册工具 + 默认钩子），
然后启动交互式对话。

运行前提：
  设置环境变量 DEEPSEEK_API_KEY（在 https://platform.deepseek.com 申请），
  或在本目录的 .env 文件中配置（两者任一即可，已设置的环境变量优先）。
  可选 DEEPSEEK_MODEL，默认 deepseek-v4-flash。
  仅用 Python 标准库，无需 pip install 任何东西。

用法：
  python main.py
"""
import config  # noqa: F401  触发 .env 加载与配置读取
from core.loop import agent_loop
from tools import (  # noqa: F401  触发工具注册 + 默认钩子注册（副作用）
    bash,
    edit,
    get_environment,
    glob,
    read,
    write,
)

from config import DEEPSEEK_API_KEY


def main() -> None:
    if not DEEPSEEK_API_KEY:
        print("未检测到 DEEPSEEK_API_KEY。可通过以下任一方式配置：")
        print("1. 设置环境变量，例如（PowerShell）：")
        print('      $env:DEEPSEEK_API_KEY = "你的key"')
        print("2. 在本项目目录创建 .env 文件（推荐，已被 git 忽略）：")
        print('      DEEPSEEK_API_KEY=你的key')
        print("   可选在 .env 中配置：DEEPSEEK_MODEL=deepseek-v4-pro")
        raise SystemExit(1)

    question = input("\n请输入你的问题：\n> ")
    agent_loop(question)


if __name__ == "__main__":
    main()
