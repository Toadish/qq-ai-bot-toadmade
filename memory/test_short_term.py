# -*- coding: utf-8 -*-
"""
test_short_term.py — 短期记忆（滑动窗口 + 压缩摘要）测试
==========================================================
运行：..\websocket\.venv\Scripts\python.exe test_short_term.py

覆盖：窗口滚动压缩 / 一级→二级滚动 / 上下文组装与预算裁剪 /
      压缩回调被调用 / 压缩失败降级 / 状态持久化往返 / flush 收尾。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from short_term import ShortTermMemory  # noqa: E402

CALLS = {"n": 0}


async def fake_compress(turns, instruction: str) -> str:
    """计数型假压缩器：模拟 LLM 摘要。"""
    CALLS["n"] += 1
    texts = " ".join(t.get("text", "")[:12] for t in turns[:2])
    return f"[摘要{len(turns)}轮] {texts}"


async def t_window_roll():
    CALLS["n"] = 0
    stm = ShortTermMemory(compress_fn=fake_compress, window_turns=4,
                          summary_chunk_turns=2, max_level1_summaries=99)
    for i in range(6):
        await stm.add_turn("user", f"用户{i}", f"消息{i}")
    assert len(stm.window) == 4, f"窗口应剩 4 轮，实际 {len(stm.window)}"
    assert len(stm.level1) == 1, f"一级摘要应为 1 条，实际 {len(stm.level1)}"
    assert CALLS["n"] == 1
    print("PASS 滑动窗口滚动压缩（溢出部分压成一级摘要）")


async def t_level_roll():
    stm = ShortTermMemory(compress_fn=fake_compress, window_turns=2,
                          summary_chunk_turns=2, max_level1_summaries=2,
                          level2_chunk_summaries=2, max_level2_summaries=3)
    for i in range(30):
        await stm.add_turn("user", "u", f"m{i}")
    assert len(stm.level1) <= 2, f"一级摘要应 ≤2，实际 {len(stm.level1)}"
    assert 1 <= len(stm.level2) <= 3, f"二级摘要应在 1~3，实际 {len(stm.level2)}"
    print(f"PASS 一级→二级滚动压缩（level1={len(stm.level1)}, level2={len(stm.level2)}）")


async def t_context_and_budget():
    stm = ShortTermMemory(window_turns=10, summary_chunk_turns=5,
                          max_level1_summaries=99, summary_max_chars=100,
                          context_budget_chars=2000)
    for i in range(25):
        await stm.add_turn("user", "某人", "x" * 50)
    # 宽预算：摘要层 + 窗口都在
    ctx = stm.context_for_prompt()
    assert "【最近对话】" in ctx and "【近期的对话摘要】" in ctx
    assert len(ctx) <= 2000, f"预算应为 2000，实际 {len(ctx)}"
    # 紧预算（300，放不下窗口）：应优先裁掉摘要层，窗口保留
    tight = stm.context_for_prompt(max_chars=300)
    assert "【近期的对话摘要】" not in tight, "紧预算下摘要层应被裁剪"
    assert "【最近对话】" in tight, "窗口应尽量保留"
    assert len(tight) <= 300, f"紧预算应为 300，实际 {len(tight)}"
    print(f"PASS 上下文组装与预算裁剪（宽 {len(ctx)} 字 / 紧 {len(tight)} 字，摘要层优先被裁）")


async def t_compress_failure_fallback():
    async def broken(turns, instruction):
        raise RuntimeError("LLM 挂了")
    stm = ShortTermMemory(compress_fn=broken, window_turns=2,
                          summary_chunk_turns=2, max_level1_summaries=99)
    for i in range(6):
        await stm.add_turn("user", "u", f"m{i}")
    assert len(stm.level1) >= 1, "失败后应退化为拼接摘要"
    assert "m0" in stm.level1[0], "拼接摘要应保留原文内容"
    print("PASS 压缩失败降级（拼接不崩、信息不丢）")


async def t_state_roundtrip():
    stm = ShortTermMemory(compress_fn=fake_compress, window_turns=3,
                          summary_chunk_turns=2, max_level1_summaries=99)
    for i in range(8):
        await stm.add_turn("user", "u", f"m{i}")
    path = Path(tempfile.mkdtemp(prefix="stm_test_")) / "session.json"
    stm.save(path)
    stm2 = ShortTermMemory.load(path, compress_fn=fake_compress)
    assert stm.to_state() == stm2.to_state()
    print("PASS 状态持久化往返一致")


async def t_flush():
    stm = ShortTermMemory(compress_fn=fake_compress, window_turns=99,
                          summary_chunk_turns=10, max_level1_summaries=99)
    for i in range(5):
        await stm.add_turn("user", "u", f"m{i}")
    text = await stm.flush()
    assert not stm.window
    assert len(stm.level1) == 1
    assert "m0" in text  # 假压缩器取前两轮文本，摘要内容应进入上下文
    assert "【近期的对话摘要】" in text
    print("PASS flush 收尾（窗口清空并压成最后一条摘要）")


def run(fn, label):
    asyncio.run(fn())
    return label


if __name__ == "__main__":
    for fn in [t_window_roll, t_level_roll, t_context_and_budget,
               t_compress_failure_fallback, t_state_roundtrip, t_flush]:
        run(fn, fn.__name__)
    print("\n全部测试通过 ✅")
