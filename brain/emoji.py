# -*- coding: utf-8 -*-
"""emoji.py — 表情包库（"搜罗"与"使用"）

两个来源：
  1. 投放库 images/emoji/：用户放进去的静态表情，文件名即关键词
     （如 "猫猫-开心-睡觉.png"，按分隔符拆词，回复文本命中即用）；
  2. 收藏库 images/collected/：机器人从聊天里"搜罗"来的图片
     （收到图片消息时自动入库），按内容哈希去重，限量限大小。

使用规则（防风控/防刷屏）：
  - 每 min_interval_messages 条回复最多带一次表情；
  - 两次表情至少间隔 30 秒；
  - 关键词没命中时，偶尔（低概率）从收藏库里随机取一张。
"""

import hashlib
import random
import re
import shutil
import time
from pathlib import Path

IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
WORD_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")


def _words(text: str) -> list:
    """拆词（只用于文件名建索引：文件名有分隔符，切得开）。"""
    return [w.lower() for w in WORD_RE.findall(text) if w]


def _kw_in_text(kw: str, text_lower: str) -> bool:
    """关键词是否命中文本：
    中文（含 CJK）→ 子串匹配（中文无空格，"开心"直接出现在句子里）；
    纯 ASCII → 词边界匹配（避免 happy 误中 unhappy）。"""
    if kw.isascii():
        return re.search(rf"(?<![0-9A-Za-z]){re.escape(kw)}(?![0-9A-Za-z])", text_lower) is not None
    return kw in text_lower


class EmojiLibrary:
    def __init__(self, root, collect: bool = True, max_files: int = 300,
                 max_file_bytes: int = 10 * 1024 * 1024,
                 min_interval_messages: int = 4,
                 cooldown_seconds: float = 30.0,
                 collected_chance: float = 0.05):
        self.root = Path(root)
        self.emoji_dir = self.root / "emoji"
        self.collected_dir = self.root / "collected"
        self.emoji_dir.mkdir(parents=True, exist_ok=True)
        self.collected_dir.mkdir(parents=True, exist_ok=True)
        self.collect = collect
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.min_interval = min_interval_messages
        self.cooldown_seconds = cooldown_seconds
        self.collected_chance = collected_chance
        self._index: dict[str, list[Path]] = {}
        self._collected: list[Path] = []
        self._messages_since_emoji = 0
        self._last_pick_ts = float("-inf")  # 负无穷：启动后第一次挑选不受冷却限制
        self.refresh()

    # ---------- 索引 ----------

    def refresh(self) -> None:
        """扫描图片库重建关键词索引。文件名按非中英文数字字符拆词。"""
        self._index.clear()
        for f in self.emoji_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMG_SUFFIXES:
                for w in _words(f.stem):
                    self._index.setdefault(w, []).append(f)
        self._collected = [
            f for f in self.collected_dir.iterdir()
            if f.is_file() and f.suffix.lower() in IMG_SUFFIXES
        ]

    # ---------- 挑选 ----------

    def keywords(self, limit: int = 0) -> list[str]:
        """投放库的全部关键词（去重排序）。limit>0 时截断（供提示词注入）。"""
        kws = sorted(self._index.keys())
        return kws[:limit] if limit > 0 else kws

    def _allow_pick(self) -> bool:
        """节奏门控：每 min_interval 条回复最多一次 + 冷却时间。"""
        self._messages_since_emoji += 1
        if self._messages_since_emoji < self.min_interval:
            return False
        if time.monotonic() - self._last_pick_ts < self.cooldown_seconds:
            return False
        return True

    def _mark_pick(self) -> None:
        self._messages_since_emoji = 0
        self._last_pick_ts = time.monotonic()

    def pick_keyword(self, keyword: str) -> Path | None:
        """按 LLM 给出的关键词挑一张表情（同样的节奏门控）。

        未知关键词直接返回 None（不消耗门控计数），调用方可回退到文本匹配。
        """
        if not keyword or keyword not in self._index:
            return None
        if not self._allow_pick():
            return None
        choice = random.choice(self._index[keyword])
        self._mark_pick()
        return choice

    def pick(self, text: str) -> Path | None:
        """按回复文本挑一张表情（关键词命中 / 偶尔随机收藏图）；没命中返回 None。"""
        if not self._allow_pick():
            return None

        hits: list[Path] = []
        seen = set()
        text_lower = text.lower()
        for kw, files in self._index.items():
            if _kw_in_text(kw, text_lower):
                for f in files:
                    if f not in seen:
                        seen.add(f)
                        hits.append(f)
        choice = None
        if hits:
            choice = random.choice(hits)
        elif self._collected and random.random() < self.collected_chance:
            # 关键词没命中：偶尔随机用一张收藏来的图（"自主使用"）
            choice = random.choice(self._collected)
        if choice is None:
            return None
        self._mark_pick()
        return choice

    # ---------- 收藏（"搜罗"） ----------

    def collect_image(self, src, keywords=()) -> Path | None:
        """收藏聊天里收到的图片（本地路径）：哈希去重、限量、限大小。"""
        if not self.collect:
            return None
        src = Path(src)
        if not src.is_file():
            return None
        size = src.stat().st_size
        if size <= 0 or size > self.max_file_bytes:
            return None
        if len(self._collected) >= self.max_files:
            return None
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        target = self.collected_dir / f"{digest}{src.suffix.lower()}"
        if not target.exists():
            shutil.copy2(src, target)
            self._collected.append(target)
        return target

    def collect_bytes(self, blob: bytes, suffix: str = ".jpg") -> Path | None:
        """收藏内存中的图片字节（网络下载场景）：哈希去重、限量、限大小。"""
        if not self.collect or not blob:
            return None
        if len(blob) > self.max_file_bytes:
            return None
        if len(self._collected) >= self.max_files:
            return None
        suffix = ("." + suffix.lstrip(".")).lower()
        if suffix not in IMG_SUFFIXES:
            suffix = ".jpg"
        digest = hashlib.sha256(blob).hexdigest()[:16]
        target = self.collected_dir / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(blob)
            self._collected.append(target)
        return target
