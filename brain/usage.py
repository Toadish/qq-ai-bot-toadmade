# -*- coding: utf-8 -*-
"""usage.py — token 用量记账（"话费"）

按自然日累计调用消耗的 token，持久化到 JSON。
当日用量超过 日预算 × warn_ratio 时，当天只提醒一次（避免刷屏），
提醒会以"盘缠/话费不够"的方式由角色转达给大人。
"""

import json
from datetime import date
from pathlib import Path


class TokenUsage:
    def __init__(self, path, daily_budget_tokens: int = 200000,
                 warn_ratio: float = 0.8):
        self.path = Path(path)
        self.budget = int(daily_budget_tokens)
        self.ratio = float(warn_ratio)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # 文件损坏：重置
        return {"date": str(date.today()), "used": 0, "warned": False}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, usage: dict) -> None:
        """记一次调用（DeepSeek 返回的 usage 字段，取 total_tokens）。"""
        tokens = int(usage.get("total_tokens") or 0)
        if tokens <= 0:
            return
        today = str(date.today())
        if self.data["date"] != today:
            self.data = {"date": today, "used": 0, "warned": False}
        self.data["used"] += tokens
        self._save()

    def warning_pending(self) -> bool:
        """是否该提醒"话费不够"：当日首次超过 预算×比例 时返回 True（一次性）。"""
        if self.data["date"] != str(date.today()):
            return False
        if self.data["warned"]:
            return False
        if self.data["used"] >= self.budget * self.ratio:
            self.data["warned"] = True
            self._save()
            return True
        return False

    def summary(self) -> str:
        used = self.data["used"]
        pct = used / self.budget if self.budget else 0.0
        return f"今日已用 {used:,}/{self.budget:,} tokens（{pct:.0%}）"
