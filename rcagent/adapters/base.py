"""数据源统一接口:RCA 信息采集工具与具体系统解耦。

v1 只提供一个 demo 实现(读本地日志目录,demo_adapter 见后);真实系统
(内部日志平台 / CMDB / 监控指标库)按本接口实现一个子类放进本包,再用环境变量
切换即可,上层的 rca_tools 采集工具代码一行都不用改。

注意:所有方法必须"纯只读"——RCA 的红线是只做诊断,处置留给人类。
"""
import abc
import os

# 数据源实现按 名称 -> 工厂 注册,便于热插拔
_ADAPTER_FACTORIES: dict[str, callable] = {}


def register_adapter(kind: str, factory: callable) -> None:
    """注册一个数据源实现;kind 对环境变量 RCA_ADAPTER。"""
    _ADAPTER_FACTORIES[kind.strip().lower()] = factory


class DataSource(abc.ABC):
    """只读数据源接口。实现类不得有写/改系统状态的副作用。"""

    name: str = "base"

    @abc.abstractmethod
    def list_entities(self, filter_text: str = "") -> list[str]:
        """列出可诊断的实体 id(作业/服务/节点/日志文件),filter_text 可模糊过滤。"""

    @abc.abstractmethod
    def query_logs(self, entity_id: str, keyword: str = "",
                   limit: int = 50, level: str = "") -> list[str]:
        """返回实体 entity_id 的日志行列表;keyword 非空则只返回包含它的行;
        level 非空则只返回该级别;limit 限行数。"""

    @abc.abstractmethod
    def get_entity_detail(self, entity_id: str) -> str:
        """返回实体的概要信息文本(状态/配置/来源),供控制器了解实体全貌。"""


_adapter: DataSource | None = None


def get_adapter() -> DataSource:
    """返回当前配置的数据源单例。未知实现抛 RuntimeError(避免静默走错数据源)。"""
    global _adapter
    if _adapter is not None:
        return _adapter
    kind = os.environ.get("RCA_ADAPTER", "demo").strip().lower()
    factory = _ADAPTER_FACTORIES.get(kind)
    if factory is None:
        raise RuntimeError(
            f"未知数据源适配器: {kind!r}。可设 RCA_ADAPTER=demo(本地日志目录)运行;"
            "接入真实系统后调用 base.register_adapter('name', ...) 注册实现即可。")
    _adapter = factory()
    return _adapter


def reset_adapter() -> None:
    """清空数据源单例(测试用)。"""
    global _adapter
    _adapter = None