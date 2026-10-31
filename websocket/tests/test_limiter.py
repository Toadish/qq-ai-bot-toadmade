# -*- coding: utf-8 -*-
"""
test_limiter.py — SendLimiter 前摇与发送间隔离线测试
=====================================================
运行：..\..\.venv\Scripts\python.exe test_limiter.py

覆盖：发送前随机前摇（第一条消息同样生效）、发送后按字数延迟仍生效。
用小参数验证时间行为，全程不到 2 秒。
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # websocket 目录

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from channel import SendLimiter  # noqa: E402


class FakeChannel:
    """记录每条消息实际发出的时刻。"""

    def __init__(self):
        self.times = []

    async def send(self, message):
        self.times.append(time.monotonic())


async def t_pre_delay():
    ch = FakeChannel()
    lim = SendLimiter(
        ch,
        base_delay_seconds=0.05,
        chars_per_second=1000.0,
        jitter_min=1.0,
        jitter_max=1.0,
        pre_delay_min_seconds=0.20,
        pre_delay_max_seconds=0.20,  # 相等 = 固定前摇，便于断言
    )
    t0 = time.monotonic()
    await lim.send("第一条")          # 入队即返回
    await asyncio.sleep(1.0)          # 给 worker 时间
    await lim.stop(drain_timeout=5)
    assert len(ch.times) == 1, "应发出 1 条"
    elapsed = ch.times[0] - t0
    # 发送必须发生在前摇之后（0.20s），且不能晚太多（上限留 0.6s 调度余量）
    assert 0.20 - 0.02 <= elapsed <= 0.20 + 0.6, f"前摇不符合预期：{elapsed:.3f}s"
    print("PASS 前摇：发送前随机停顿生效（第一条消息同样等待）")


async def t_spacing():
    ch = FakeChannel()
    lim = SendLimiter(
        ch,
        base_delay_seconds=0.30,
        chars_per_second=1000.0,
        jitter_min=1.0,
        jitter_max=1.0,
        pre_delay_min_seconds=0.05,
        pre_delay_max_seconds=0.05,
    )
    await lim.send("a")
    await lim.send("b")
    await asyncio.sleep(1.5)
    await lim.stop(drain_timeout=5)
    assert len(ch.times) == 2, f"应发出 2 条，实际 {len(ch.times)}"
    gap = ch.times[1] - ch.times[0]
    # 发后间隔 = base(0.30) + 下一条的前摇(0.05)，必须 ≥ 0.30（留 0.05 容差）
    assert gap >= 0.30 - 0.05, f"发后间隔被破坏：{gap:.3f}s"
    print("PASS 发后间隔：按字数延迟在加入前摇后仍然生效")


async def t_zero_pre_delay():
    # 前摇为 0 时行为不变：入队后尽快发出（回归保护）
    ch = FakeChannel()
    lim = SendLimiter(
        ch,
        base_delay_seconds=0.02,
        chars_per_second=1000.0,
        jitter_min=1.0,
        jitter_max=1.0,
        pre_delay_min_seconds=0.0,
        pre_delay_max_seconds=0.0,
    )
    t0 = time.monotonic()
    await lim.send("快")
    await asyncio.sleep(0.6)
    await lim.stop(drain_timeout=5)
    assert len(ch.times) == 1 and ch.times[0] - t0 < 0.5
    print("PASS 零前摇：pre_delay=0 时行为与旧版一致")


if __name__ == "__main__":
    asyncio.run(t_pre_delay())
    asyncio.run(t_spacing())
    asyncio.run(t_zero_pre_delay())
    print("\n全部测试通过 ✅")
