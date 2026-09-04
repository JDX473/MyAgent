"""内置工具集合：统一 import 本包即完成所有工具的 @tool 注册。

新增工具模块后，在这里加一行 import 即可被 Agent 使用。
"""
from tools.bash_tool import bash
from tools.code_tools import read_code, search_code, search_symbol
from tools.env_tool import get_environment
from tools.file_tools import edit, glob, read, write
from tools.hooks_setup import (  # noqa: F401  默认钩子注册（副作用）
    _on_model_response,
    _on_plan_drift_counter,
    _on_plan_progress,
    _on_plan_reminder,
    _on_plan_stop_summary,
    _on_plan_summary_at_submit,
    _on_post_tool,
    _on_stop_token_summary,
    _permission_check,
)
from tools.planner_tools import get_plan, plan_task, revise_plan, update_step
from tools.rca_tools import search_logs
from tools.subagent_tool import subagent
from tools.websearch_tool import websearch
