"""数据源适配器包:base(接口) + demo(本地日志) + im-logsearch(QuantumLink 日志平台)。"""
from rcagent.adapters.base import (  # noqa: F401
    DataSource,
    get_adapter,
    register_adapter,
    reset_adapter,
)
from rcagent.adapters import demo_adapter  # noqa: F401  导入即注册 demo 实现
from rcagent.adapters import im_logsearch  # noqa: F401  导入即注册 im-logsearch 实现