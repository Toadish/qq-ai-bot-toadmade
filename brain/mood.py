# -*- coding: utf-8 -*-
"""
mood.py — 情绪状态（总情绪 + 按人分化，喜怒哀惧）
====================================================
两级情绪：
  1. 总情绪（心境）——她此刻的整体状态，键 _GLOBAL；
  2. 按人分化（分情绪）——她对每个具体的人，键 = QQ 号。

核心机制：
  - 每次决策 LLM 顺带报 global_mood_delta / mood_delta，单次限幅；
  - 分情绪：随时间线性衰减回基线（速度 = decay_per_hour × calm_factor，
    calm_factor 是角色级平复因子，来自人设 meta.json）；
  - 总情绪：**在线时**受"自然回基线 + 所有联系人把心境拉向各自分情绪"
    两条合力驱动；**离线时（下机）只自然回基线**，不受任何人牵引。
    拉力按四个因子随好感度变化：好感度越高，喜的拉力越强、
    怒/哀/惧的拉力越弱（"在意的人更能让你开心，也更难让你真的生气"）；
  - 上机继承：有记忆记录的人 → 分情绪直接继承其**长期情绪**
    （memory.json 人物层的 mood 字段，下机时按 long_term_merge 缓慢混入，
    制造"看见一个人就来气"）；没有记录的人 → 继承当前总体心境；
  - 时间一律来自时间源（防改系统时钟）。

数据文件：brain/mood.json（paths.mood_file 指定）。
"""

import json
from datetime import datetime
from pathlib import Path

DIMS = ("喜", "怒", "哀", "惧")
GLOBAL = "_global"     # 总情绪的存储键
STATE = "_state"       # 在线/离线状态存储键

DEFAULT_BASELINE = {"喜": 10, "怒": 5, "哀": 10, "惧": 15}   # 她底子里偏静、惧略高

# 分情绪 → 总心境 的拉力参数（实机体验后再微调）
DEFAULT_INFLUENCE = {
    "influence_base_positive": 0.06,   # 喜 的基础拉力（/小时）
    "influence_base_negative": 0.05,   # 怒/哀/惧 的基础拉力（/小时）
    "favor_scale": 0.08,               # 好感度每 +1：喜拉力 ×(1+s)、负面拉力 ×(1-s)
    "favor_clamp": 10.0,               # 好感度参与权重时的上下限
}


class MoodStore:
    """总情绪 + 按 QQ 分化的情绪存储。

    clock：时间源（北京时间 datetime；None = 时间不可用，跳过按时间的结算）。
    favor_of：callable(qq) -> 好感度数值（供拉力加权；缺省视为 0）。
    """

    def __init__(self, path, clock=None, decay_per_hour: float = 5.0,
                 calm_factor: float = 1.0, max_delta: float = 5.0,
                 baseline: dict | None = None, favor_of=None, **influence):
        self.path = Path(path)
        self._clock = clock
        self.decay_per_hour = float(decay_per_hour)
        self.calm_factor = float(calm_factor)
        self.max_delta = float(max_delta)
        self.baseline = dict(DEFAULT_BASELINE)
        if baseline:
            self.baseline.update({k: float(v) for k, v in baseline.items()})
        self._favor_of = favor_of or (lambda qq: 0.0)
        cfg = dict(DEFAULT_INFLUENCE)
        cfg.update({k: float(v) for k, v in influence.items() if k in DEFAULT_INFLUENCE})
        self.w_pos = cfg["influence_base_positive"]
        self.w_neg = cfg["influence_base_negative"]
        self.favor_scale = cfg["favor_scale"]
        self.favor_clamp = cfg["favor_clamp"]
        self.data = self._load()

    # ---------------- 持久化 ----------------

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- 时间 ----------------

    def _now(self):
        if self._clock is None:
            return None
        try:
            return self._clock()
        except Exception:
            return None

    @staticmethod
    def _hours_since(entry, now) -> float:
        if now is None or not entry.get("updated"):
            return 0.0
        try:
            last = datetime.fromisoformat(entry["updated"])
            return max(0.0, (now - last).total_seconds() / 3600.0)
        except (ValueError, TypeError):
            return 0.0

    def _stamp(self, entry, now) -> None:
        if now is not None:
            entry["updated"] = now.isoformat(timespec="seconds")

    def _online(self) -> bool:
        state = self.data.get(STATE) or {}
        return bool(state.get("online", True))   # 首次运行默认在线

    # ---------------- 条目 ----------------

    def _entry(self, key: str) -> dict:
        if key not in self.data:
            # 新联系人：没有记录的人直接继承当前总体心境
            g = self.data.get(GLOBAL) or {}
            gm = g.get("mood") if isinstance(g.get("mood"), dict) else None
            mood0 = {d: float(gm.get(d, self.baseline[d])) for d in DIMS} if gm \
                else dict(self.baseline)
            self.data[key] = {"name": None, "mood": mood0, "updated": None}
        entry = self.data[key]
        entry.setdefault("mood", dict(self.baseline))
        for dim in DIMS:
            entry["mood"].setdefault(dim, self.baseline[dim])
        return entry

    # ---------------- 结算（衰减/拉力） ----------------

    def _settle_person(self, key: str, now) -> None:
        """分情绪：线性衰减回基线（在线离线都一样随时间流逝）。"""
        entry = self.data.get(key)
        if entry is None:
            return
        hours = self._hours_since(entry, now)
        if hours <= 0:
            return
        step = self.decay_per_hour * self.calm_factor * hours
        for dim in DIMS:
            base = self.baseline[dim]
            value = float(entry["mood"][dim])
            if value > base:
                value = max(base, value - step)
            else:
                value = min(base, value + step)
            entry["mood"][dim] = round(value, 2)
        self._stamp(entry, now)

    def _weight(self, dim: str, favor: float) -> float:
        """四因子：好感度越高，喜拉力越强、负面拉力越弱。"""
        f = max(-self.favor_clamp, min(self.favor_clamp, float(favor or 0.0)))
        if dim == "喜":
            return self.w_pos * (1.0 + self.favor_scale * f)
        return self.w_neg * max(0.2, 1.0 - self.favor_scale * f)

    def _settle_global(self, now, online: bool) -> None:
        """总心境：自然回基线 +（在线时）所有联系人把心境拉向各自分情绪。

        按 ≤1 小时步长推进（线性近似，稳定不振荡）：每步向
        目标 T_d = (c·基线 + Σ w_i·P_i) / (c + Σw_i) 移动 (c+Σw)·dt 距离。
        """
        entry = self._entry(GLOBAL)
        hours = self._hours_since(entry, now)
        if hours <= 0:
            return
        c = self.decay_per_hour * self.calm_factor
        persons = [(k, e) for k, e in self.data.items()
                   if k not in (GLOBAL, STATE) and isinstance(e.get("mood"), dict)]
        remaining = hours
        for _ in range(48):          # 最多推进 48 步（每小时一步，再久视为收敛）
            if remaining <= 0:
                break
            dt = min(1.0, remaining)
            remaining -= dt
            for dim in DIMS:
                value = float(entry["mood"][dim])
                if online:
                    pull, wsum = 0.0, 0.0
                    for key, e in persons:
                        w = self._weight(dim, self._favor_of(key))
                        pull += w * float(e["mood"][dim])
                        wsum += w
                    denom = c + wsum
                    target = (c * self.baseline[dim] + pull) / denom if denom > 0 \
                        else self.baseline[dim]
                    rate = denom
                else:
                    # 离线：只自然回基线，不受任何人的情绪牵引
                    target = self.baseline[dim]
                    rate = c
                step = rate * dt
                if value < target:
                    value = min(target, value + step)
                else:
                    value = max(target, value - step)
                entry["mood"][dim] = round(max(0.0, min(100.0, value)), 2)
        self._stamp(entry, now)

    def _settle_current(self) -> None:
        """按当前在线/离线模式把一切结算到现在（先人后总）。"""
        now = self._now()
        if now is None:
            return
        for key in list(self.data.keys()):
            if key not in (GLOBAL, STATE):
                self._settle_person(key, now)
        self._settle_global(now, self._online())

    # ---------------- 在线/离线切换 ----------------

    def set_online(self, online: bool) -> None:
        """切换在线状态。切换前先按旧模式结算到当前时刻，
        保证"离线时段 = 纯自然衰减、在线时段 = 带众人牵引"。"""
        self._settle_current()
        self.data.setdefault(STATE, {})["online"] = bool(online)
        now = self._now()
        if now is not None:
            self.data[STATE]["at"] = now.isoformat(timespec="seconds")
        self.save()

    # ---------------- 上机继承 ----------------

    def init_from_people(self, people: dict) -> None:
        """上机：有记录的人 → 分情绪继承其长期情绪；没记录的人 → 重置为总体心境。"""
        self._settle_current()
        now = self._now()
        people = {str(k): v for k, v in (people or {}).items()}
        for key, entry in list(self.data.items()):
            if key in (GLOBAL, STATE):
                continue
            person = people.get(key)
            lm = person.get("mood") if person else None
            if isinstance(lm, dict):
                for dim in DIMS:
                    entry["mood"][dim] = float(lm.get(dim, self.baseline[dim]))
            else:
                g = self.data.get(GLOBAL) or {}
                gm = g.get("mood") if isinstance(g.get("mood"), dict) else None
                for dim in DIMS:
                    entry["mood"][dim] = float(
                        (gm or {}).get(dim, self.baseline[dim]))
            if person and person.get("name"):
                entry["name"] = person["name"]
            self._stamp(entry, now)
        self.save()

    # ---------------- 应用变化 / 注入 ----------------

    def _apply(self, entry: dict, delta: dict, name=None) -> None:
        if not isinstance(delta, dict):
            return
        self._settle_current()
        now = self._now()
        self._stamp(entry, now)
        if name:
            entry["name"] = name
        for dim in DIMS:
            raw = delta.get(dim)
            if raw is None:
                continue
            try:
                change = float(raw)
            except (TypeError, ValueError):
                continue
            change = max(-self.max_delta, min(self.max_delta, change))
            value = float(entry["mood"][dim]) + change
            entry["mood"][dim] = round(max(0.0, min(100.0, value)), 2)
        self.save()

    def global_line(self) -> str:
        self._settle_current()
        entry = self._entry(GLOBAL)
        return self._line(entry)

    def global_values(self) -> dict:
        """总心境的数值快照（先结算），供事件判定用。"""
        self._settle_current()
        return {dim: float(self._entry(GLOBAL)["mood"][dim]) for dim in DIMS}

    def values_for(self, qq: str) -> dict:
        """某人的分情绪数值快照（先结算该人），供事件判定用。"""
        key = str(qq)
        self._settle_person(key, self._now())
        return {dim: float(self._entry(key)["mood"][dim]) for dim in DIMS}

    def line_for(self, qq: str) -> str:
        now = self._now()
        key = str(qq)
        self._settle_person(key, now)
        return self._line(self._entry(key))

    def _line(self, entry: dict) -> str:
        return "、".join(f"{dim}{int(entry['mood'][dim])}" for dim in DIMS)

    def apply_global_delta(self, delta: dict) -> None:
        self._apply(self._entry(GLOBAL), delta)

    def apply_delta(self, qq: str, name, delta: dict) -> None:
        self._apply(self._entry(str(qq)), delta, name=name)

    def per_person_snapshot(self) -> dict:
        """各人的当前分情绪（{qq: {"mood": {...}}}），供下机混入长期情绪。"""
        self._settle_current()
        return {
            k: {"mood": dict(e["mood"])}
            for k, e in self.data.items()
            if k not in (GLOBAL, STATE)
        }

    def snapshot(self) -> dict:
        """全部情绪快照（调试/测试用）。"""
        return json.loads(json.dumps(self.data, ensure_ascii=False))
