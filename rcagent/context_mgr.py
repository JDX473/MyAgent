"""OBSK 快照机制:压缩长工具结果,避免撑爆上下文。

参考 RCAgent(arXiv:2310.16340) 的 Observation Snapshot Key 设计:
长观察数据(日志、表条目)不整段交给控制器,而是:

  1. 全文存入本模块的 SnapshotStore(key-value store);
  2. 只把观察的 head + 快照键([snapshot: <key>])展示给控制器;
  3. 控制器需要全文时,通过 get_snapshot / analyze_logs 取回或让专家分析。

风格:纯标准库;store 为进程内全局单例(LRU 有界),本质是"可丢弃的缓存",
跨会话复用无副作用;/clear 不清也不影响正确性。想要干净会话时调 reset()。
"""
import hashlib
import os

# ---- 容量/行为参数(可用环境变量覆盖,便于单测与部署调优) ----
_SNAP_MAX = int(os.environ.get("RCA_SNAPSHOT_MAX", "50"))                    # store 最多保留条数
_SNAP_ENTRY_MAX = int(os.environ.get("RCA_SNAPSHOT_ENTRY_MAX", "200000"))    # 单条字符上限
_SNAP_THRESHOLD = int(os.environ.get("RCA_SNAPSHOT_THRESHOLD", "2000"))      # 超过此长度才压缩
_SNAP_HEAD = int(os.environ.get("RCA_SNAPSHOT_HEAD", "800"))                 # 压缩后展示的 head 字符数

SNAP_PREFIX = "snap-"
# RCA 垂直会话名:钩子只对它生效,避免污染通用 session(见 rca_hooks)
RCA_SESSION_NAME = "RCA"

# 不做自动压缩的工具:结果本身就是快照内容,或不应被截断(错误/报告)
_NO_AUTO_SNAPSHOT = {"get_snapshot", "finalize", "get_environment", "analyze_logs"}


class SnapshotStore:
    """键 -> 全文 的快照存储:条数上限 + LRU 淘汰 + 按内容去重。

    纯缓存,任意一条丢失都不影响正确性(丢了 get_snapshot 会报"快照不存在",
    模型重新采集即可)。
    """

    def __init__(self, max_entries: int = _SNAP_MAX, entry_max: int = _SNAP_ENTRY_MAX) -> None:
        self._store: dict[str, str] = {}
        self._by_hash: dict[str, str] = {}   # 内容 md5 -> key,避免重复存同一内容
        self._order: list[str] = []          # 最近使用顺序(末尾最新,淘汰首部)
        self._counter = 0
        self.max_entries = max_entries
        self.entry_max = entry_max

    def _touch(self, key: str) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def _evict(self) -> None:
        while len(self._store) > self.max_entries and self._order:
            oldest = self._order.pop(0)
            content = self._store.pop(oldest, None)
            if content is not None:
                h = hashlib.md5(content.encode("utf-8", "replace")).hexdigest()
                self._by_hash.pop(h, None)

    def put(self, content: str) -> str | None:
        """存入全文,返回快照键;内容为空或超过单条上限时返回 None。"""
        if not content:
            return None
        if len(content) > self.entry_max:
            return None
        h = hashlib.md5(content.encode("utf-8", "replace")).hexdigest()
        existing = self._by_hash.get(h)
        if existing is not None:
            self._touch(existing)
            return existing
        self._counter += 1
        key = f"{SNAP_PREFIX}{self._counter:04d}"
        self._store[key] = content
        self._by_hash[h] = key
        self._order.append(key)
        self._evict()
        return key

    def get(self, key: str) -> str | None:
        """按快照键取全文;不存在返回 None。"""
        content = self._store.get(key)
        if content is None:
            return None
        self._touch(key)
        return content

    def __len__(self) -> int:
        return len(self._store)


_store: SnapshotStore | None = None


def snapshot_store() -> SnapshotStore:
    """全局单例(进程内共享,有界缓存)。"""
    global _store
    if _store is None:
        _store = SnapshotStore()
    return _store


def reset() -> None:
    """清空快照(主要用于测试与 /clear 场景)。"""
    global _store
    _store = None


def auto_snapshot(ctx) -> None:
    """POST_TOOL_EXECUTE 钩子:工具结果过长时,全文入 store,只回填 head + 快照键。

    替换 ctx.result —— loop 回填进 messages 用的是 ctx.result(见 core/loop.py),
    因此控制器只看到压缩后的文本;全文通过 get_snapshot / analyze_logs 取。
    只对 RCA 垂直会话生效(ctx.name == "RCA")。
    """
    if ctx.name != RCA_SESSION_NAME:
        return
    if ctx.tool_name in _NO_AUTO_SNAPSHOT:
        return
    result = ctx.result or ""
    if len(result) <= _SNAP_THRESHOLD:
        return
    key = snapshot_store().put(result)
    if key is None:
        return  # 超上限存不进(几乎不会),那就保持原样
    head = result[:_SNAP_HEAD]
    ctx.result = (
        f"{head}\n"
        f"...(全文共 {len(result)} 字符,已存入快照 [snapshot: {key}],"
        f"需要完整内容请调用 get_snapshot('{key}') 取片段,"
        f"或 analyze_logs('{key}', ...) 让日志专家深度分析)"
    )