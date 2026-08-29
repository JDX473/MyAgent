"""RCA 垂直 Agent 包:import 本包即完成 工具注册 + 钩子挂载(副作用,仿 tools/ 包)。

只被 `python main_rca.py` 入口 import,不影响 `python main.py` 的通用 Agent。
"""
# 1) 数据源适配器(注册 demo 实现)
from rcagent import adapters  # noqa: F401

# 2) OBSK 快照 + JSON 稳定化(核心机制)
from rcagent import context_mgr  # noqa: F401
from rcagent import stabilization  # noqa: F401

# 3) RCA 工具注册(list_entities/query_logs/…/finalize/analyze_logs)
from rcagent import rca_tools  # noqa: F401
from rcagent import log_expert  # noqa: F401

# 4) RCA 专有钩子(重复调用检测 / 过早 finalize 打回 / finalize 终止)
from rcagent.rca_hooks import register_rca_hooks  # noqa: F401
register_rca_hooks()

# 5) 让 run_tool 的参数解析在失败时走 JSON 修复(对所有工具生效)
from core.tools import set_json_repair  # noqa: E402
from rcagent.stabilization import repair_json  # noqa: E402
set_json_repair(repair_json)

# 便捷导出
from rcagent.rca_prompts import (  # noqa: F401, E402
    RCA_SYSTEM_PROMPT,
    RESPONSIBILITY_RULES,
)
from rcagent.rca_tools import parse_responsibility  # noqa: F401, E402