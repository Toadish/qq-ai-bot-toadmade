# -*- coding: utf-8 -*-
"""
test_timesource.py — 时间源离线测试（假 fetcher，不发真实网络请求）
===================================================================
运行：..\..\websocket\.venv\Scripts\python.exe test_timesource.py

覆盖：事件时间优先与过期、联网兜底与端点降级、缓存节流、全失败返回 None、
      异常事件时间忽略、三种端点格式解析。
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from brain.timesource import BEIJING, TimeSource  # noqa: E402

EXPECTED = datetime(2026, 10, 29, 16, 30, tzinfo=BEIJING)
UNIX = int(EXPECTED.timestamp())


async def fetcher_ok(url):
    if "taobao" in url:
        return {"data": {"t": str(int(EXPECTED.timestamp() * 1000))}}
    if "timeapi.io" in url:
        # 与真实接口一致的扁平字段
        return {"year": 2026, "month": 10, "day": 29, "hour": 16,
                "minute": 30, "seconds": 0, "milliSeconds": 0}
    if "worldtimeapi" in url:
        return {"unixtime": UNIX}
    raise AssertionError(f"意外端点 {url}")


async def fetcher_half(url):
    if "timeapi.io" in url:
        return {"year": 2026, "month": 10, "day": 29, "hour": 16,
                "minute": 30, "seconds": 0}
    raise ConnectionError(f"端点挂了 {url}")


async def fetcher_fail(url):
    raise ConnectionError("网络全挂")


def t_event_time_first():
    ts = TimeSource(cfg={"cache_seconds": 300}, fetcher=fetcher_fail)
    ts.observe_event_time(UNIX)
    assert ts.last_known() == EXPECTED
    print("PASS 事件时间优先：喂入后立即可取（不联网）")


def t_event_time_expiry():
    # 缓存 0.1 秒：立即读取新鲜，稍后过期 → 兜底联网
    ts = TimeSource(cfg={"cache_seconds": 0.1}, fetcher=fetcher_ok)
    ts.observe_event_time(UNIX)
    assert ts.last_known() == EXPECTED   # 刚喂入：新鲜
    time.sleep(0.2)                      # 已过缓存期
    assert ts.last_known() is None       # 过期 → 不可用
    asyncio.run(ts.refresh())            # 联网兜底
    assert ts.last_known() == EXPECTED   # 恢复
    print("PASS 过期兜底：事件时间过期后转联网查询")


def t_fetch_fallback_order():
    ts = TimeSource(cfg={"cache_seconds": 300}, fetcher=fetcher_half)
    asyncio.run(ts.refresh())
    assert ts.last_known() == EXPECTED
    print("PASS 端点降级：前一个端点失败自动试下一个")


def t_all_fail_none():
    ts = TimeSource(cfg={"cache_seconds": 300}, fetcher=fetcher_fail)
    asyncio.run(ts.refresh())
    assert ts.last_known() is None
    print("PASS 全失败：last_known 返回 None（调用方跳过日更操作）")


def t_throttle():
    calls = {"n": 0}

    async def counting(url):
        calls["n"] += 1
        return {"data": {"t": str(int(EXPECTED.timestamp() * 1000))}}

    ts = TimeSource(cfg={"cache_seconds": 300}, fetcher=counting)
    asyncio.run(ts.refresh())
    asyncio.run(ts.refresh())   # 缓存期内 → 不重复联网
    assert calls["n"] == 1, f"应只联网一次，实际 {calls['n']} 次"
    print("PASS 缓存节流：缓存期内重复 refresh 不联网")


def t_invalid_event_time():
    ts = TimeSource(cfg={"cache_seconds": 300}, fetcher=fetcher_fail)
    ts.observe_event_time(0)
    ts.observe_event_time(None)
    ts.observe_event_time("不是时间")
    ts.observe_event_time(99999999999999)
    assert ts.last_known() is None
    print("PASS 异常事件时间：0/None/非数字/超范围一律忽略")


def t_parse_formats():
    ts = TimeSource()
    assert ts._parse("https://x.taobao.com/y",
                     {"data": {"t": str(int(EXPECTED.timestamp() * 1000))}}) == EXPECTED
    # timeapi.io 真实返回的扁平字段
    assert ts._parse("https://timeapi.io/z",
                     {"year": 2026, "month": 10, "day": 29, "hour": 16,
                      "minute": 30, "seconds": 0}) == EXPECTED
    # 兼容 dateTime 形态（含 Z 后缀与超 6 位小数秒）
    assert ts._parse("https://timeapi.io/z", {"dateTime": "2026-10-29T16:30:00"}) == EXPECTED
    from datetime import datetime as _dt
    assert ts._parse("https://timeapi.io/z",
                     {"dateTime": "2026-10-29T16:30:00.1234567Z"}) == \
        _dt(2026, 10, 29, 16, 30, 0, 123456, tzinfo=BEIJING)
    assert ts._parse("https://worldtimeapi.org/z", {"unixtime": UNIX}) == EXPECTED
    assert ts._parse("https://unknown", {"anything": 1}) is None
    assert ts._parse("https://x.taobao.com/y", {"data": {"t": "垃圾"}}) is None
    assert ts._parse("https://timeapi.io/z", {"year": "坏"}) is None
    print("PASS 解析：三种端点格式（含 timeapi 扁平字段）+ 未知/坏数据返回 None")


if __name__ == "__main__":
    t_event_time_first()
    t_event_time_expiry()
    t_fetch_fallback_order()
    t_all_fail_none()
    t_throttle()
    t_invalid_event_time()
    t_parse_formats()
    print("\n全部测试通过 ✅")
