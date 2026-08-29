"""demo 数据源:从本地目录读取日志文件,不接真实系统也能端到端跑通 RCA。

- 日志数据目录由环境变量 RCA_DEMO_DATA_DIR 指定(默认 ./rca_demo_data);
- 目录下每个 *.log / *.txt 文件视为一个"实体",文件名即实体 id;
- query_logs 按行返回(尾部优先,近似"最近的日志"),keyword 做子串过滤;
- 纯只读,无任何副作用。

接入真实系统时:在 rcagent/adapters/ 下照 base.DataSource 写一个实现,
注册后设 RCA_ADAPTER=<你的名字> 即可,上层工具代码不变。
"""
import glob
import os

from rcagent.adapters.base import DataSource, register_adapter

_TEXT_EXT = (".log", ".txt", ".err", ".out", ".jsonl")


class DemoDataSource(DataSource):
    name = "demo"

    def __init__(self, data_dir: str | None = None) -> None:
        self.dir = data_dir or os.environ.get("RCA_DEMO_DATA_DIR") or \
            os.path.join(os.getcwd(), "rca_demo_data")

    # ---- 工具 ----
    def _file_or_none(self, entity_id: str) -> str | None:
        """把实体 id 解析为文件路径(允许带相对子目录),找不到返回 None。"""
        if not entity_id or os.path.basename(entity_id) == entity_id:
            candidate = os.path.join(self.dir, entity_id)
        else:
            candidate = os.path.normpath(os.path.join(self.dir, entity_id))
        if os.path.isfile(candidate):
            return candidate
        # 兜底:按 basename 搜索
        for f in self._files():
            if os.path.basename(f) == os.path.basename(entity_id):
                return f
        return None

    def _files(self) -> list[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(
            f for f in glob.glob(os.path.join(self.dir, "**", "*"), recursive=True)
            if os.path.isfile(f) and f.lower().endswith(_TEXT_EXT)
        )

    def list_entities(self, filter_text: str = "") -> list[str]:
        names = [os.path.relpath(f, self.dir) for f in self._files()]
        if filter_text:
            q = filter_text.lower()
            names = [n for n in names if q in n.lower()]
        return names

    def query_logs(self, entity_id: str, keyword: str = "",
                   limit: int = 50, level: str = "") -> list[str]:
        path = self._file_or_none(entity_id)
        if not path:
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        if keyword:
            q = keyword.lower()
            lines = [l for l in lines if q in l.lower()]
        if level:
            # 只保留包含目标级别记号的行(INFO/WARN/ERROR/FATAL)
            q = level.upper()
            lines = [l for l in lines if q in l.upper()]
        return lines[-limit:]  # 取尾部(近似最近)

    def get_entity_detail(self, entity_id: str) -> str:
        path = self._file_or_none(entity_id)
        if not path:
            return f"实体不存在: {entity_id}(demo 数据源目录 {self.dir} 无此文件)"
        with open(path, encoding="utf-8", errors="replace") as f:
            n_lines = sum(1 for _ in f)
        return (
            f"来源: demo 数据源(本地日志文件)\n"
            f"文件: {path}\n"
            f"大小: {os.path.getsize(path)} 字节\n"
            f"行数: {n_lines}\n"
            f"提示: 用 query_logs('{entity_id}', keyword=...) 按关键词过滤查看"
        )


# 导入本模块即注册为 RCA_ADAPTER=demo 的实现
register_adapter("demo", DemoDataSource)