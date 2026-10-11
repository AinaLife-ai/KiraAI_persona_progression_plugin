import json
import os
import time
from pathlib import Path
from typing import Dict, Any


class StateManager:
    """会话状态管理（阶段 / 轮数计数 / 连续保持计数）。

    读写口径与 v1.1.5 **完全一致**（states.json 结构不变，老用户状态原样加载）。
    改动只有落盘方式（性能与安全，对外不可见）：
      · **原子写**：tmp 文件 + os.replace，崩溃不会留下半文件；
      · **脏标记防抖**：高频计数（每条消息批次）只标脏，由
        flush() / 关键变更（阶段切换）/ 周期兜底 落盘，
        热路径不再每批消息全量写盘。
    """

    #: 周期兜底落盘间隔（秒）。阶段变更等关键操作仍立即落盘。
    AUTO_FLUSH_INTERVAL = 30.0

    def __init__(self, data_dir: Path, scope: str = "session"):
        self.data_dir = Path(data_dir)
        self.scope = scope
        self.state_file = self.data_dir / "states.json"
        self._states: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._last_flush = time.monotonic()
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._states = data if isinstance(data, dict) else {}
            except Exception:
                self._states = {}
        else:
            self._states = {}

    # ---------- 落盘 ----------
    def _write_atomic(self):
        """立即落盘（原子）。老版本每次变更都调用本方法。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._states, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_file)
        self._dirty = False
        self._last_flush = time.monotonic()

    def _save(self, critical: bool = False):
        """落盘入口。critical=True（阶段切换等）立即写；否则标脏按周期兜底。"""
        if critical:
            self._write_atomic()
        else:
            self._dirty = True

    def flush(self, force: bool = False):
        """冲刷脏数据。周期兜底与插件终止时调用。"""
        if not self._dirty:
            return
        if force or (time.monotonic() - self._last_flush) >= self.AUTO_FLUSH_INTERVAL:
            try:
                self._write_atomic()
            except Exception:
                # 兜底失败不致命：保留下次再试
                pass

    # ---------- 状态访问（口径与旧版一致） ----------
    def _get_key(self, session_id: str) -> str:
        if self.scope == "global":
            return "global"
        return session_id

    def get_state(self, session_id: str) -> Dict[str, Any]:
        key = self._get_key(session_id)
        return self._states.get(key, {})

    def update_state(self, session_id: str, updates: Dict[str, Any], *, critical: bool = False):
        key = self._get_key(session_id)
        if key not in self._states:
            self._states[key] = {}
        self._states[key].update(updates)
        self._save(critical=critical)

    def get_stage(self, session_id: str, default_stage: int = 0) -> int:
        return self.get_state(session_id).get("stage", default_stage)

    def set_stage(self, session_id: str, stage: int):
        # 阶段是关键数据：立即落盘（崩溃也不许丢阶段）
        self.update_state(session_id, {"stage": stage}, critical=True)

    def get_turn_counter(self, session_id: str) -> int:
        return self.get_state(session_id).get("turn_counter", 0)

    def set_turn_counter(self, session_id: str, value: int):
        self.update_state(session_id, {"turn_counter": value})

    def increment_turn(self, session_id: str):
        cur = self.get_turn_counter(session_id)
        self.set_turn_counter(session_id, cur + 1)

    def reset_turn_counter(self, session_id: str):
        self.set_turn_counter(session_id, 0)

    def get_consecutive_stay(self, session_id: str) -> int:
        return self.get_state(session_id).get("consecutive_stay", 0)

    def set_consecutive_stay(self, session_id: str, value: int):
        self.update_state(session_id, {"consecutive_stay": value})
