# -*- coding: utf-8 -*-
"""
short_term.py — 短期记忆（滑动窗口 + 压缩摘要）
================================================
单次「上机 → 下机」区间内的对话记忆，解决"上下文装不下整段聊天"的问题：

    最近 window_turns 轮对话保留原文（细粒度、可引用）；
    更早的对话按 summary_chunk_turns 轮一组，滚动压缩成「一级摘要」；
    一级摘要积满 max_level1_summaries 条后，按 level2_chunk_summaries 条
    一组再压缩成「二级摘要」；
    二级超限时最旧两条合并（信息不丢，只降保真度）。

压缩由聊天大脑的 LLM 完成：本模块只负责「何时压、压什么、怎么拼」，
通过 compress_fn 回调接入（异步）。压缩失败时退化为文本拼接，不会崩。

与长期记忆的分工：
    短期记忆 = 一次聊天内部的上下文，下机即失效（可存档用于崩溃恢复）；
    长期记忆 = 跨聊天沉淀的回忆（memory.py：遗忘 / 计数 / 晋升）。
    注意：下机时的长期总结应基于【完整原始聊天日志】，而不是本模块的摘要——
    摘要有损，不适合做长期记忆的唯一原料。
"""

import json
from collections import deque
from pathlib import Path

DEFAULT_PARAMS = {
    "window_turns": 12,           # 滑动窗口：最近 N 轮保留原文
    "summary_chunk_turns": 10,    # 滚出窗口的对话每 N 轮压成一条一级摘要
    "max_level1_summaries": 5,    # 一级摘要上限，超出滚动压成二级
    "level2_chunk_summaries": 4,  # 几条一级摘要压成一条二级摘要
    "max_level2_summaries": 3,    # 二级摘要上限，超出时最旧两条合并
    "summary_max_chars": 300,     # 单条摘要的字符硬上限
    "context_budget_chars": 4000, # 注入提示词的预算，超出丢最旧摘要
}


class ShortTermMemory:
    """单会话短期记忆。用法：

        stm = ShortTermMemory(compress_fn=my_llm_compress)  # 或 **config["short_term"]
        await stm.add_turn("user", "某人", "晚上好", channel="群123")
        await stm.add_turn("assistant", "妖刀姬", "晚上好。")
        system_prompt += stm.context_for_prompt()

    compress_fn 签名：async def fn(turns: list[dict], instruction: str) -> str
    不传时使用内置降级压缩（文本拼接），方便离线开发与测试。
    """

    def __init__(self, compress_fn=None, window_turns=12, summary_chunk_turns=10,
                 max_level1_summaries=5, level2_chunk_summaries=4,
                 max_level2_summaries=3, summary_max_chars=300,
                 context_budget_chars=4000):
        self.p = {
            "window_turns": window_turns,
            "summary_chunk_turns": summary_chunk_turns,
            "max_level1_summaries": max_level1_summaries,
            "level2_chunk_summaries": level2_chunk_summaries,
            "max_level2_summaries": max_level2_summaries,
            "summary_max_chars": summary_max_chars,
            "context_budget_chars": context_budget_chars,
        }
        self.window = deque()          # 最近对话原文
        self.level1 = []               # 一级摘要（近期）
        self.level2 = []               # 二级摘要（较早）
        self._compress = compress_fn or self._fallback_compress

    # ---------------- 输入 ----------------

    async def add_turn(self, role: str, name: str, text: str, channel: str = "") -> None:
        """记一轮对话。窗口溢出时同步压缩滚出的部分（每 N 轮多一次 LLM 摘要
        调用，约 2~5 秒，可接受；保证任何时刻记忆不出现空洞）。"""
        self.window.append({"role": role, "name": name, "text": text, "channel": channel})
        while len(self.window) > self.p["window_turns"]:
            chunk = []
            for _ in range(self.p["summary_chunk_turns"]):
                if not self.window:
                    break
                chunk.append(self.window.popleft())
            if chunk:
                summary = await self._compress_safe(chunk, self._turns_instruction(chunk))
                await self._push_level1(summary)

    async def flush(self) -> str:
        """会话收尾：把窗口剩余对话压成最后一条摘要，返回完整上下文文本。
        供崩溃恢复存档 / 需要全量回顾时使用。"""
        if self.window:
            chunk = list(self.window)
            self.window.clear()
            summary = await self._compress_safe(chunk, self._turns_instruction(chunk))
            await self._push_level1(summary)
        return self.context_for_prompt()

    # ---------------- 滚动压缩（内部） ----------------

    async def _push_level1(self, summary: str) -> None:
        self.level1.append(summary)
        while len(self.level1) > self.p["max_level1_summaries"]:
            chunk = self.level1[: self.p["level2_chunk_summaries"]]
            del self.level1[: len(chunk)]
            merged = await self._compress_safe(
                self._wrap_summaries(chunk), self._summaries_instruction(chunk)
            )
            await self._push_level2(merged)

    async def _push_level2(self, summary: str) -> None:
        self.level2.append(summary)
        while len(self.level2) > self.p["max_level2_summaries"]:
            pair = self.level2[:2]
            del self.level2[:2]
            merged = await self._compress_safe(
                self._wrap_summaries(pair), self._merge_instruction(pair)
            )
            self.level2.append(merged)

    # ---------------- 输出 ----------------

    def context_for_prompt(self, max_chars=None) -> str:
        """组装可注入 system prompt 的上下文文本（摘要最旧在前、窗口最新在后）。

        超预算时按「最旧优先」裁剪：先丢最旧的二级摘要 → 再丢最旧的一级摘要
        → 最后才从窗口最旧一轮截断（加「[…前略…]」标记）。
        """
        budget = max_chars or self.p["context_budget_chars"]
        l2, l1 = list(self.level2), list(self.level1)
        win_lines = [self._fmt_turn(t) for t in self.window]

        def assemble(lines):
            parts = []
            if l2:
                parts.append("【较早的对话摘要】\n" + "\n".join(l2))
            if l1:
                parts.append("【近期的对话摘要】\n" + "\n".join(l1))
            if lines:
                parts.append("【最近对话】\n" + "\n".join(lines))
            return "\n\n".join(parts)

        text = assemble(win_lines)
        while len(text) > budget and l2:
            l2.pop(0)
            text = assemble(win_lines)
        while len(text) > budget and l1:
            l1.pop(0)
            text = assemble(win_lines)
        while len(text) > budget and len(win_lines) > 1:
            win_lines.pop(0)
            win_lines[0] = "[…前略…] " + win_lines[0]
            text = assemble(win_lines)
        return text

    # ---------------- 压缩指令模板（给 LLM） ----------------

    @staticmethod
    def _fmt_turn(t: dict) -> str:
        loc = f"[{t['channel']}] " if t.get("channel") else ""
        return f"{loc}{t['name']}：{t['text']}"

    def _turns_instruction(self, turns) -> str:
        joined = "\n".join(self._fmt_turn(t) for t in turns)
        return (
            f"请把下面这段聊天记录压缩成不超过 {self.p['summary_max_chars']} 字的摘要。"
            "保留：谁说了什么、关键事实与承诺、未解决的话题、情绪基调。"
            "不要编造原文没有的内容。\n\n" + joined
        )

    def _summaries_instruction(self, texts) -> str:
        return (
            f"请把以下多条对话摘要合并成一条不超过 {self.p['summary_max_chars']} 字的摘要，"
            "保留重要事实与人物：\n" + "\n".join(texts)
        )

    def _merge_instruction(self, texts) -> str:
        return (
            f"请把以下两条对话摘要合并为一条不超过 {self.p['summary_max_chars']} 字的摘要：\n"
            + "\n".join(texts)
        )

    @staticmethod
    def _wrap_summaries(texts: list[str]) -> list[dict]:
        return [{"role": "summary", "name": "", "text": s, "channel": ""} for s in texts]

    # ---------------- 压缩回调 ----------------

    async def _fallback_compress(self, turns, instruction: str) -> str:
        """无 LLM 时的降级压缩：直接拼接（离线开发/测试用）。"""
        return "；".join(t.get("text", "") for t in turns)

    async def _compress_safe(self, turns, instruction: str) -> str:
        """调用压缩回调；失败时退化为拼接并截断，保证模块永不因压缩崩溃。"""
        try:
            result = await self._compress(turns, instruction)
            text = (result or "").strip()
            if not text:
                raise ValueError("压缩结果为空")
        except Exception as exc:
            print(f"[STM] 压缩失败，退化为拼接：{exc!r}")
            text = "；".join(t.get("text", "") for t in turns)
        return text[: self.p["summary_max_chars"]]

    # ---------------- 持久化（崩溃恢复） ----------------

    def to_state(self) -> dict:
        return {
            "window": list(self.window),
            "level1": list(self.level1),
            "level2": list(self.level2),
        }

    @classmethod
    def from_state(cls, state: dict, **kwargs) -> "ShortTermMemory":
        stm = cls(**kwargs)
        stm.window = deque(state.get("window", []))
        stm.level1 = list(state.get("level1", []))
        stm.level2 = list(state.get("level2", []))
        return stm

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_state(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path, **kwargs) -> "ShortTermMemory":
        path = Path(path)
        state = {}
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_state(state, **kwargs)
