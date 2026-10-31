# -*- coding: utf-8 -*-
"""
timesource.py — 时间源（生产路径绝不用本地电脑时间）
=====================================================
妖刀姬需要"感知时间"：回复里带时辰、按天做遗忘/晋升等日更操作。
本地电脑时间可以被改（刷好感度 / 刷每日上限），所以一律不用它。

取值优先级：
  1. OneBot 事件时间 —— NapCat 在每条报文里带 `time`（Unix 秒）。
     消息来一次刷新一次，是最可信、最新的来源（回复时差只有几秒）。
  2. 联网北京时间 —— 事件时间过旧（或无事件）时，按 endpoints 顺序
     依次尝试查询，成功后缓存 cache_seconds 秒，到期前不重复联网。
  3. 都拿不到 —— last_known() 返回 None，调用方跳过依赖日期的操作。

用法：
    ts = TimeSource(cfg)              # cfg = config 的 timesource 组（可空）
    ts.observe_event_time(event_time) # 每条 OneBot 事件进来时喂入（同步）
    await ts.refresh()                # 上机 / 下机前刷新（联网兜底，异步）
    now = ts.last_known()             # 同步取当前北京时间；拿不到返回 None
    await ts.close()                  # 释放连接
"""

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

# 统一 +08:00（Asia/Shanghai 无夏令时，固定偏移完全等价；
# 不用 zoneinfo，避免 Windows 上缺 tzdata 包）
BEIJING = timezone(timedelta(hours=8))

# 各端点返回的 JSON 结构不同，按域名分派解析
_ENDPOINT_DEFAULTS = [
    "https://api.m.taobao.com/rest/api3.do?api=mtop.common.getTimestamp",
    "https://timeapi.io/api/Time/current/zone?timeZone=Asia/Shanghai",
    "http://worldtimeapi.org/api/timezone/Asia/Shanghai",
]

# 合理的 Unix 秒范围（2020-01-01 ~ 2100-01-01）：挡住 0/None/异常值
_MIN_UNIX = 1577836800
_MAX_UNIX = 4102444800


class TimeSource:
    """事件时间优先、联网北京时间兜底的时间源。

    内部只保存"单调钟 + 对应北京时间"两个缓存槽：
      _event_cache —— 最近一次 OneBot 事件时间（最可信）
      _net_cache   —— 最近一次联网查询结果（防止频繁联网）
    last_known() 按新鲜度取其一，都在缓存期内过期时返回 None。
    """

    def __init__(self, cfg: dict | None = None, fetcher=None):
        cfg = cfg or {}
        self._endpoints = list(cfg.get("endpoints") or _ENDPOINT_DEFAULTS)
        self._timeout = float(cfg.get("timeout_seconds", 5))
        self._cache_seconds = float(cfg.get("cache_seconds", 300))
        self._fetcher = fetcher          # 测试注入：async (url) -> dict
        self._client = None
        self._event_cache = None         # (monotonic, datetime)
        self._net_cache = None           # (monotonic, datetime)

    # ---------------- 喂入 / 查询 ----------------

    def observe_event_time(self, unix_seconds) -> None:
        """记录一条 OneBot 事件的 `time` 字段（Unix 秒）。"""
        try:
            ts = int(unix_seconds)
        except (TypeError, ValueError):
            return
        if not (_MIN_UNIX <= ts <= _MAX_UNIX):
            return
        dt = datetime.fromtimestamp(ts, BEIJING)
        self._event_cache = (time.monotonic(), dt)

    def last_known(self):
        """同步取当前北京时间（naive, +08:00）；拿不到返回 None。"""
        now_mono = time.monotonic()
        for cache in (self._event_cache, self._net_cache):
            if cache is None:
                continue
            taken_at, dt = cache
            # 用 < 不用 <=：cache_seconds=0 时立即过期（本机 monotonic 粒度约
            # 15.6ms，同刻调用差值可能是 0，<= 会让 0 秒缓存永不过期）
            if now_mono - taken_at < self._cache_seconds:
                return dt
        return None

    async def refresh(self) -> None:
        """确保手里有一个尽可能新的时间：缓存过期时才联网查询。"""
        if self.last_known() is not None:
            return
        dt = await self._fetch_net_time()
        if dt is not None:
            self._net_cache = (time.monotonic(), dt)

    # ---------------- 联网兜底 ----------------

    async def _fetch_net_time(self):
        """按顺序尝试 endpoints，返回北京时间 datetime；全失败返回 None。"""
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
        for url in self._endpoints:
            try:
                if self._fetcher is not None:
                    payload = await self._fetcher(url)
                else:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                dt = self._parse(url, payload)
                if dt is not None:
                    return dt
            except Exception:
                continue    # 某个端点失败 → 试下一个
        return None

    @staticmethod
    def _parse(url: str, payload) -> datetime | None:
        """按端点格式把响应解析成北京时间 datetime；不认识的返回 None。"""
        if not isinstance(payload, dict):
            return None
        try:
            if "taobao" in url:
                # {"data": {"t": "1750...ms"}}
                ms = int(str(payload["data"]["t"]))
                return datetime.fromtimestamp(ms / 1000, BEIJING)
            if "timeapi.io" in url:
                # 真实返回同时带扁平字段与 dateTime；优先用扁平字段（无歧义）。
                if "year" in payload and "month" in payload and "day" in payload:
                    return datetime(
                        int(payload["year"]), int(payload["month"]), int(payload["day"]),
                        int(payload["hour"]), int(payload["minute"]),
                        int(payload.get("seconds", 0) or 0), tzinfo=BEIJING,
                    )
                # 兜底：dateTime 字符串。兼容 "Z" 后缀与超 6 位的小数秒
                # （Python 3.10 的 fromisoformat 两者都不认）。
                if "dateTime" in payload:
                    text = str(payload["dateTime"]).replace("Z", "+00:00")
                    if "." in text:
                        base, _, rest = text.partition(".")
                        frac, i = "", 0
                        while i < len(rest) and rest[i].isdigit():
                            if len(frac) < 6:
                                frac += rest[i]
                            i += 1
                        text = base + (("." + frac) if frac else "") + rest[i:]
                    return datetime.fromisoformat(text).replace(tzinfo=BEIJING)
            if "worldtimeapi" in url:
                # {"unixtime": 1750...}  ← UTC 纪元秒
                unixtime = int(payload["unixtime"])
                return datetime.fromtimestamp(unixtime, BEIJING)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
