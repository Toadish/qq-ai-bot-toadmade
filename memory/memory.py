# -*- coding: utf-8 -*-
"""
memory.py — 长期记忆引擎（模拟人类遗忘与唤醒）
================================================
按「memory/SKILL.md」的规格实现。机制一句话：
    下机时总结出最多 N 条关键回忆（每条 ≤ 字节上限）；
    每日遗忘仪式（每天只执行一次）：每条普通回忆有 20% 概率被遗忘，
    没被忘的计数 +1；聊天中有人准确提起某条回忆，计数再 +1（每条每天限一次）；
    计数达到阈值(30) → 晋升为「珍贵回忆」，另存；
    每次有回忆新晋珍贵，既有珍贵回忆各有 10% 概率被遗忘；
    对重要/感兴趣的人维护标签列与好感度，由人设自主增删加减。

日更与防作弊（二期）：
    - 遗忘仪式只在「每天第一次上机」执行；同日多次上机不重复遗忘/计数，
      重启刷不了进度。每次下机也会检查：若连续运行跨天没重启，下机时
      补做当天的遗忘仪式。
    - 提起加分每条回忆每天限加 mention_bonus_per_day 次，且**跨重启持久化**
      （存 memory.json 的 meta），反复重启也无法重复刷分。
    - 日期一律来自注入的 clock（生产环境 = 时间源：OneBot 事件时间 →
      联网北京时间，绝不用本地电脑时间）；clock 返回 None 时跳过所有
      依赖日期的操作。

稳态（数学上自洽，按"天"计）：
    普通回忆 ≈ 每天新增 / 遗忘率 0.2 = 50 条（每天新增 10 条时）；
    珍贵回忆 ≈ 1 / 0.1 = 10 条。

数据文件：personas/<slug>/memory.json（换人设只换目录，引擎通用）。
"""

import json
import random
import uuid
from pathlib import Path

# 出厂默认参数；memory.json 里的 params 可覆盖（调参只需改数据文件）
DEFAULT_PARAMS = {
    "per_session_max_new": 10,      # 每次下机最多写入多少条新回忆
    "max_memory_bytes": 128,        # 每条回忆的 UTF-8 字节上限（约 42 个汉字）
    "ordinary_forget_rate": 0.20,   # 每日遗忘仪式时，每条普通回忆的遗忘概率
    "precious_forget_rate": 0.10,   # 新回忆晋升时，既有珍贵回忆的遗忘概率
    "precious_threshold": 15,       # 计数达到该值 → 晋升珍贵回忆
    "mention_bonus_per_day": 1,     # 被准确提起时，每条回忆每天限加的计数（二期起按天计）
}


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，且不切断多字节字符（中文不会变成乱码）。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[:max_bytes]
    while True:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]  # 去掉末尾被切断的半个字符


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class MemoryStore:
    """长期记忆存储：普通回忆 / 珍贵回忆 / 人物档案 三层。

    生命周期钩子（由机器人主循环调用）：
        session_start()        上机：每日遗忘仪式（每天只执行一次）
        mark_mentioned(ids)    聊天中：有人准确提起回忆 → 计数 +1（每条每天限一次）
        session_end(texts)     下机：写入本次总结出的关键回忆（跨天时补做当日仪式）
        update_person(...)     人设自主维护人物标签与好感度

    clock：可调用对象，返回北京时间 datetime（None = 时间不可用）。
    生产环境传时间源（事件时间 → 联网北京时间），绝不使用本地时钟；
    不传 clock 时所有日更操作跳过（防止误用本地时间刷进度）。
    """

    def __init__(self, path: str | Path, params: dict | None = None, seed=None,
                 clock=None):
        self.path = Path(path)
        self.rng = random.Random(seed)      # 生产环境不传 seed；测试传 seed 复现
        self._clock = clock                 # 时间源；None = 时间不可用
        self.data = None
        self._load(params)

    # ---------------- 时间 ----------------

    def _now(self):
        """当前北京时间（由时间源提供）；None = 时间不可用。"""
        if self._clock is None:
            return None
        try:
            return self._clock()
        except Exception:
            return None

    def _today(self):
        dt = self._now()
        return dt.date() if dt is not None else None

    def _now_iso(self):
        dt = self._now()
        return dt.isoformat(timespec="seconds") if dt is not None else None

    # ---------------- 持久化 ----------------

    def _load(self, params: dict | None) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"schema": 1, "ordinary": [], "precious": [], "people": {}}
        merged = dict(DEFAULT_PARAMS)
        if params:
            merged.update(params)
        merged.update(self.data.get("params", {}))   # 数据文件里的参数优先级最高
        # 兼容旧参数名：mention_bonus_per_session（每会话）→ mention_bonus_per_day（每天）
        if "mention_bonus_per_day" not in merged and "mention_bonus_per_session" in merged:
            merged["mention_bonus_per_day"] = merged["mention_bonus_per_session"]
        self.data.setdefault("ordinary", [])
        self.data.setdefault("precious", [])
        self.data.setdefault("people", {})
        self.data.setdefault("meta", {})
        # 发送通道清单（主动事件选取用）：群通道 + 在意的人的私聊通道。
        # 来源：同意入群申请/邀请时建群通道、新建在意的人标签时建私聊通道；
        # 旧数据回填：已有的人物层全部视为私聊通道。
        if "channels" not in self.data:
            self.data["channels"] = {
                "groups": [],
                "private": sorted(self.data["people"].keys()),
            }
        self.data.setdefault("channels", {}).setdefault("groups", [])
        self.data["channels"].setdefault("private", [])
        self.data["params"] = merged
        self.params = merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- 上机 / 下机 ----------------

    def _daily_forget(self, today) -> bool:
        """每日遗忘仪式（同一天只执行一次）：遗忘 → 幸存者计数 +1 → 查晋升。

        返回是否真的执行了（False = 今天已执行过）。
        """
        meta = self.data.setdefault("meta", {})
        if str(meta.get("last_forget_date")) == str(today):
            return False
        kept = []
        for mem in self.data["ordinary"]:
            if self.rng.random() < self.params["ordinary_forget_rate"]:
                continue                    # 遗忘：直接丢弃
            mem["count"] += 1               # 本次未被遗忘 → 计数 +1
            kept.append(mem)
        self.data["ordinary"] = kept
        self._promote_all_due()             # 计数 +1 之后再判定晋升
        meta["last_forget_date"] = str(today)
        self.save()
        return True

    def session_start(self) -> dict:
        """上机：仅在日期自上次遗忘仪式后变化时执行（先遗忘后计数，再查晋升）。

        同日多次上机不重复遗忘/计数——日更操作以时间源日期为准，重启刷不了进度；
        时间不可用时跳过仪式（宁可不做，也不用本地时间）。
        """
        today = self._today()
        if today is None:
            print("[记忆] 时间不可用（无事件时间且联网失败），跳过今日遗忘仪式")
            return self.snapshot()
        if not self._daily_forget(today):
            print("[记忆] 今日遗忘仪式已执行过（同日再次上机），跳过")
        return self.snapshot()

    def session_end(self, new_memories: list[str]) -> int:
        """下机：检查今天产生的记忆——写入本次总结出的关键回忆。

        连续运行跨天时（未重启），这里补做当天遗忘仪式，保证"与时间挂钩"
        不因长驻而失效。返回实际写入条数。
        双重保险：最多 per_session_max_new 条、每条截断到 max_memory_bytes 字节
        （总结提示词里也会要求 LLM 遵守，这里兜底）。
        """
        today = self._today()
        if today is not None:
            self._daily_forget(today)   # 跨天兜底：不漏掉当天的遗忘仪式
            self.data.setdefault("meta", {})["last_summary_date"] = str(today)
        added = 0
        for text in new_memories:
            if added >= self.params["per_session_max_new"]:
                break
            text = (text or "").strip()
            if not text:
                continue
            self.data["ordinary"].append({
                "id": _new_id(),
                "text": _truncate_utf8(text, self.params["max_memory_bytes"]),
                "count": 0,
                "created": self._now_iso(),
            })
            added += 1
        self.save()
        return added

    # ---------------- 聊天中 ----------------

    def mark_mentioned(self, memory_ids: list[str]) -> int:
        """聊天中有人准确提起回忆 → 计数 +1（每条回忆【每天】限加一次，跨重启有效）。

        由「聊天大脑」做语义匹配（哪条消息对应哪条回忆），把命中的 id 传进来；
        本方法只负责计数与限次。限次记录持久化在 memory.json 的 meta 里，
        反复重启也刷不了分。返回实际加分的条数。
        """
        today = self._today()
        if today is None:
            return 0    # 时间不可用：不加分（宁可不加分，也不让人靠重启刷）
        meta = self.data.setdefault("meta", {})
        bonus = meta.setdefault("mention_bonus", {"date": None, "counts": {}})
        if str(bonus.get("date")) != str(today):
            bonus["date"] = str(today)
            bonus["counts"] = {}
        cap = int(self.params.get("mention_bonus_per_day", 1))
        credited = 0
        for mid in memory_ids:
            if int(bonus["counts"].get(mid, 0)) >= cap:
                continue
            mem = self._find(mid)
            if mem is None:
                continue
            mem["count"] += 1
            bonus["counts"][mid] = int(bonus["counts"].get(mid, 0)) + 1
            credited += 1
        if credited:
            self._promote_all_due()
            self.save()
        return credited

    # ---------------- 人物层 ----------------

    def update_person(self, qq: str, name=None, labels_add=(), labels_del=(),
                      favor_delta=0) -> None:
        """人设自主维护人物档案：增删标签、加减好感度。

        例：store.update_person("123456", name="那个总在深夜出现的人",
                                 labels_add=["熟客"], favor_delta=2)
        新建人物标签时自动把 TA 加进私聊通道清单（主动事件选取用）。
        """
        qq = str(qq)
        is_new = qq not in self.data["people"]
        person = self.data["people"].setdefault(qq, {"labels": [], "favor": 0})
        if name:
            person["name"] = name
        labels = set(person.get("labels", []))
        labels.update(labels_add)
        labels.difference_update(labels_del)
        person["labels"] = sorted(labels)
        person["favor"] = int(person.get("favor", 0)) + int(favor_delta)
        if is_new:
            self.note_private_channel(qq, save=False)
        self.save()

    def note_group_channel(self, group_id) -> None:
        """建立/刷新一个群发送通道（她所在的群），供主动事件选取。"""
        gid = str(group_id)
        groups = self.data.setdefault("channels", {}).setdefault("groups", [])
        if gid not in groups:
            groups.append(gid)
            groups.sort()
            self.save()

    def note_private_channel(self, qq: str, save: bool = True) -> None:
        """建立/刷新一个私聊发送通道（在意的人），供主动事件选取。"""
        qq = str(qq)
        privates = self.data.setdefault("channels", {}).setdefault("private", [])
        if qq not in privates:
            privates.append(qq)
            privates.sort()
            if save:
                self.save()

    def channel_lists(self) -> tuple:
        """发送通道清单：(群号列表, 私聊 QQ 列表)。"""
        ch = self.data.get("channels") or {}
        return sorted(ch.get("groups") or []), sorted(ch.get("private") or [])

    def update_person_mood(self, qq: str, session_mood: dict, alpha: float = 0.2,
                           beta: float = 0.05, baseline: dict | None = None) -> None:
        """下机：把该人本次会话的分情绪牵引进其「长期情绪」，同时向基线回落。

        一步更新：LT ← LT + α·(S − LT) + β·(B − LT)
        （α = 分情绪牵引速率，β = 向基线的自然回落速率）
        平衡点 = (α·S + β·B)/(α+β)：反复相处才升得上去、久不出现会滑回基线，
        不会只涨不跌。基线默认取分情绪初始值（调用方传 mood 的 baseline）。
        """
        baseline = baseline or {}
        person = self.data["people"].setdefault(qq, {"labels": [], "favor": 0})
        lm = person.setdefault("mood", {})
        for dim, v in (session_mood or {}).items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            b = float(baseline.get(dim, v))
            old = float(lm.get(dim, v))
            new = old + float(alpha) * (v - old) + float(beta) * (b - old)
            lm[dim] = round(max(0.0, min(100.0, new)), 2)
        self.save()

    def remove_person(self, qq: str) -> None:
        self.data["people"].pop(qq, None)
        self.save()

    # ---------------- 查询 / 输出 ----------------

    def snapshot(self) -> dict:
        """全部记忆与人物（供组装提示词 / 调试查看）。"""
        return {
            "ordinary": list(self.data["ordinary"]),
            "precious": list(self.data["precious"]),
            "people": dict(self.data["people"]),
        }

    def format_for_prompt(self) -> str:
        """把记忆压缩成可直接注入 system prompt 的文本块（无编号，兼容旧用法）。"""
        parts = [self._memories_block(), self.format_people_for_prompt()]
        return "\n".join(p for p in parts if p)

    def _memories_block(self) -> str:
        lines = []
        if self.data["precious"]:
            lines.append("【珍贵回忆】")
            for mem in self.data["precious"]:
                lines.append(f"- {mem['text']}")
        if self.data["ordinary"]:
            lines.append("【普通回忆】")
            for mem in self.data["ordinary"]:
                lines.append(f"- {mem['text']}")
        return "\n".join(lines)

    def numbered_memories(self) -> tuple[str, dict]:
        """带会话内编号的回忆注入块 + 编号→id 映射（供「提起加分」识别用）。

        编号从 1 开始连续排（珍贵在前），只在本会话内有效；LLM 只需在
        JSON 里报编号，成本远低于报完整 id。
        """
        lines, mapping = [], {}
        n = 0
        if self.data["precious"]:
            lines.append("【珍贵回忆】")
            for mem in self.data["precious"]:
                n += 1
                lines.append(f"- [{n}] {mem['text']}")
                mapping[str(n)] = mem["id"]
        if self.data["ordinary"]:
            lines.append("【普通回忆】")
            for mem in self.data["ordinary"]:
                n += 1
                lines.append(f"- [{n}] {mem['text']}")
                mapping[str(n)] = mem["id"]
        return "\n".join(lines), mapping

    def format_people_for_prompt(self) -> str:
        """【你记得的人】注入块（带 QQ 号与长期情绪，防编造、保持态度一致）。"""
        if not self.data["people"]:
            return ""
        lines = ["【你记得的人】"]
        for qq, p in self.data["people"].items():
            tags = "、".join(p.get("labels", [])) or "无标签"
            mood_txt = ""
            if isinstance(p.get("mood"), dict) and p["mood"]:
                mood_txt = "，长期情绪 " + "、".join(
                    f"{dim}{int(p['mood'].get(dim, 0))}"
                    for dim in ("喜", "怒", "哀", "惧")
                )
            lines.append(
                f"- {p.get('name', '某人')}（QQ {qq}；{tags}{mood_txt}）"
                f"好感度：{p.get('favor', 0)}"
            )
        return "\n".join(lines)

    # ---------------- 内部 ----------------

    def _find(self, memory_id: str):
        for mem in self.data["ordinary"]:
            if mem.get("id") == memory_id:
                return mem
        for mem in self.data["precious"]:
            if mem.get("id") == memory_id:
                return mem
        return None

    def _promote_all_due(self) -> int:
        """把所有计数达标的普通回忆晋升为珍贵回忆。返回晋升条数。"""
        threshold = self.params["precious_threshold"]
        promoted = 0
        i = 0
        while i < len(self.data["ordinary"]):
            mem = self.data["ordinary"][i]
            if mem["count"] >= threshold:
                self.data["ordinary"].pop(i)
                self._promote(mem)
                promoted += 1
                # pop 后下一个元素顶到当前位置，i 不自增
            else:
                i += 1
        return promoted

    def _promote(self, mem) -> None:
        """晋升一条回忆：先对「之前已有的」珍贵回忆做遗忘判定，再收入新晋者。"""
        rate = self.params["precious_forget_rate"]
        self.data["precious"] = [
            p for p in self.data["precious"]
            if self.rng.random() >= rate
        ]
        mem["promoted"] = self._now_iso()
        self.data["precious"].append(mem)
