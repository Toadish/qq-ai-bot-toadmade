# -*- coding: utf-8 -*-
"""
proactive.py — 主动发起话题 / 发朋友圈（事件驱动）
==================================================
两个事件（条件 → 冷却 → 行动）：

  1. 无聊事件：总心境 喜、哀 都偏低 + 近 idle_seconds 秒没有收发消息。
     冷却 boredom_cooldown_seconds（5 分钟）。触发后先掷随机因子
     （角色级 boredom_chance，沉默的角色设低）决定要不要行动；
     没掷中就跳过（也计入冷却，避免每轮重掷变相提高概率）。
     行动在「群聊 / 私聊 / 发朋友圈」中随机选一项：
       - 群聊：随机选一个已知群（本会话/近期出现过的群），**不带 @**；
       - 私聊：随机选一位记忆中在意的人；
       - 发朋友圈：走 publish_qzone。
  2. 倾诉/吐槽事件：总心境任一项 > vent_threshold（60）。
     **必定行动**，在「私聊 / 发朋友圈」二选一（不去群里）。
     冷却 vent_cooldown_seconds（默认 3 小时）。

草拟文案每次行动一次 LLM 调用（带心境/时间/人物信息），简短、符合人设。
所有冷却时间戳持久化在 mood.json 的 _state（用时间源，防改系统时钟）。
"""

import asyncio
import json
import random
import time
from datetime import datetime


class ProactiveAgent:
    """挂在 ChatBrain 上的主动行为代理：后台循环 + 事件判定 + 行动。"""

    def __init__(self, brain):
        self.b = brain
        self.cfg = brain.cfg.get("proactive", {})
        self.enabled = bool(self.cfg.get("enabled", True))
        self.tick_seconds = float(self.cfg.get("tick_seconds", 60))
        self.idle_seconds = float(self.cfg.get("idle_seconds", 300))
        self.bored_cd = float(self.cfg.get("boredom_cooldown_seconds", 300))
        # 随机因子：角色级（人设 meta.json 的 boredom_chance），沉默角色设低。
        # 显式判 None：boredom_chance=0（从不主动）不能被 or 回退成默认值
        meta_chance = brain._persona_meta.get("boredom_chance")
        self.bored_chance = float(
            meta_chance if meta_chance is not None
            else self.cfg.get("boredom_chance", 0.3)
        )
        self.joy_max = float(self.cfg.get("boredom_joy_max", 20))
        self.sorrow_max = float(self.cfg.get("boredom_sorrow_max", 20))
        self.vent_threshold = float(self.cfg.get("vent_threshold", 60))
        self.vent_cd = float(self.cfg.get("vent_cooldown_seconds", 10800))
        self._task = None
        # 发送通道清单在长期记忆里（memory.json 的 channels）：
        # 群通道 = 同意入群申请/邀请时建立；私聊通道 = 新建在意的人标签时建立。
        # 旧数据一次性迁移：mood.json _state 里的 known_groups 并入群通道清单。
        state = brain.mood.data.get("_state") or {}
        old_groups = state.pop("known_groups", None)
        if old_groups and brain.store is not None:
            for gid in old_groups:
                brain.store.note_group_channel(gid)
            if not state:
                brain.mood.data.pop("_state", None)
            brain.mood.save()
        # 渠道权重：说说 0.5、私聊 1.5、群 1.0（默认，config 可调）
        default_weights = {"group": 1.0, "private": 1.5, "qzone": 0.5}
        self.weights = dict(default_weights)
        self.weights.update(self.cfg.get("channel_weights") or {})
        self._last_activity = time.monotonic()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            print(f"[主动] 已启动（每 {self.tick_seconds:.0f} 秒检查一次）")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def touch(self) -> None:
        """有收发消息时调用：重置空闲计时。"""
        self._last_activity = time.monotonic()

    def note_group(self, group_id) -> None:
        """会话中出现的群 → 回写群通道清单（她被拉进群等未走申请流程的情况）。"""
        if group_id is None:
            return
        if self.b.store is not None:
            self.b.store.note_group_channel(group_id)

    # ---------------- 主循环 ----------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self._tick()
            except Exception as exc:
                print(f"[主动] 检查异常（已忽略）：{exc!r}")

    async def _tick(self) -> None:
        b = self.b
        if not self.enabled:
            return
        if not getattr(b.channel, "connected", False):
            return
        # 空闲期间事件时间缓存会过期：先刷新时间源（内部有缓存节流，
        # 到期才联网），否则长挂机后 last_known() 恒为 None，事件永远不触发
        try:
            await b.timesource.refresh()
        except Exception as exc:
            print(f"[主动] 时间源刷新失败（本轮跳过）：{exc!r}")
            return
        now = b.timesource.last_known()
        if now is None:
            return    # 时间不可用：不做主动行为（防改系统时钟刷冷却）
        if time.monotonic() - self._last_activity < self.idle_seconds:
            return    # 最近有收发消息：安静待着
        # 先判倾诉（必定行动、冷却长）；触发后本轮不再判无聊，避免双动作
        if await self._try_venting(now):
            return
        await self._try_boredom(now)

    # ---------------- 事件判定 ----------------

    def _in_cooldown(self, key: str, seconds: float, now) -> bool:
        state = self.b.mood.data.setdefault("_state", {})
        at = state.get(key)
        if not at:
            return False
        try:
            last = datetime.fromisoformat(at)
            return (now - last).total_seconds() < seconds
        except (ValueError, TypeError):
            return False

    def _mark(self, key: str, now) -> None:
        self.b.mood.data.setdefault("_state", {})[key] = now.isoformat(timespec="seconds")
        self.b.mood.save()

    async def _try_boredom(self, now) -> None:
        v = self.b.mood.global_values()
        if not (v["喜"] <= self.joy_max and v["哀"] <= self.sorrow_max):
            return
        if self._in_cooldown("bored_at", self.bored_cd, now):
            return
        self._mark("bored_at", now)          # 每次尝试都计入冷却
        if random.random() >= self.bored_chance:
            print("[主动] 无聊条件达成，随机未选中（保持沉默）")
            return
        print(f"[主动] 无聊事件触发（心境 喜{v['喜']:.0f} 哀{v['哀']:.0f}）")
        await self._act("boredom", now)

    async def _try_venting(self, now) -> bool:
        v = self.b.mood.global_values()
        hot = [d for d in ("喜", "怒", "哀", "惧") if v[d] > self.vent_threshold]
        if not hot:
            return False
        if self._in_cooldown("vent_at", self.vent_cd, now):
            return False
        self._mark("vent_at", now)
        print(f"[主动] 倾诉事件触发（{'、'.join(hot)}偏高：{v[hot[0]]:.0f}）")
        await self._act("venting", now)
        return True

    # ---------------- 行动 ----------------

    def _channel_pool(self, mode: str) -> tuple:
        """候选渠道与权重：从长期记忆的发送通道清单里取（群通道/私聊通道）。

        私聊候选 = 通道清单 ∪ 人物层键（人物层是权威的"在意的人"来源）。
        返回 (渠道名列表, 权重列表)；渠道名顺序与群/私聊候选列表一一对应。
        """
        b = self.b
        groups, privates = b.store.channel_lists() if b.store else ([], [])
        if b.store is not None:
            privates = sorted(set(privates) | set((b.store.data.get("people") or {}).keys()))
        if mode == "venting":
            names, pool = [], []
            if privates:
                names.append("private")
                pool.append(privates)
            names.append("qzone")
            pool.append([])
        else:
            names, pool = [], []
            if groups:
                names.append("group")
                pool.append(groups)
            if privates:
                names.append("private")
                pool.append(privates)
            names.append("qzone")
            pool.append([])
        weights = [float(self.weights.get(n, 1.0)) for n in names]
        return names, weights, pool

    async def _act(self, mode: str, now) -> None:
        b = self.b
        channels, weights, pool = self._channel_pool(mode)
        if not channels:
            return
        kind = random.choices(channels, weights=weights, k=1)[0]   # 按权重抽取渠道
        candidates = pool[channels.index(kind)]
        try:
            if kind == "group":
                gid = random.choice(candidates)
                text = await self._draft("group", {"group": gid}, mode)
                if not text:
                    return
                action = json.dumps({
                    "action": "send_msg",
                    "params": {
                        "message_type": "group", "group_id": gid,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                }, ensure_ascii=False)
                await b.channel.sender.send(action)
                b._log(f"[主动·群{gid}] {b.name}：{text}")
                await b.stm.add_turn("assistant", b.name, text, channel=f"群{gid}")
                print(f"[主动] 群聊发起话题（群 {gid}）：{text[:40]}")
            elif kind == "private":
                qq = random.choice(candidates)
                text = await self._draft("private", {"qq": qq}, mode)
                if not text:
                    return
                action = json.dumps({
                    "action": "send_msg",
                    "params": {
                        "message_type": "private", "user_id": qq,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                }, ensure_ascii=False)
                await b.channel.sender.send(action)
                b._log(f"[主动·私聊{qq}] {b.name}：{text}")
                await b.stm.add_turn("assistant", b.name, text, channel="私聊")
                print(f"[主动] 私聊发起话题（QQ {qq}）：{text[:40]}")
            else:
                text = await self._draft("qzone", {}, mode)
                if not text:
                    return
                await b.publish_qzone(text)
                print(f"[主动] 发表说说：{text[:40]}")
        finally:
            self.touch()   # 自己发了消息：重置空闲计时

    # ---------------- 草拟（一次 LLM 调用） ----------------

    async def _draft(self, kind: str, context: dict, mode: str):
        """按渠道草拟文案。返回纯文本（截断 100 字）；失败返回 None。"""
        b = self.b
        lines = [ln for ln in (
            b._time_hint().strip(),
            "【你此刻的心境】" + b.mood.global_line(),
        ) if ln]
        if kind == "private" and b.store is not None:
            ppl = b.store.format_people_for_prompt()
            if ppl:
                lines.append(ppl)
        if kind == "private":
            qq = context.get("qq")
            if mode == "venting":
                instruction = (
                    f"\n请以角色身份，主动找 QQ {qq} 倾诉/吐槽一下此刻的心情。"
                    f"简短（20-40 字）、符合人设；只输出要发的话，"
                    f"不要解释、不要引号。"
                )
            else:
                instruction = (
                    f"\n请以角色身份，主动给 QQ {qq} 发一句简短的话"
                    f"（寒暄/关心/提起一件与他有关的事）。"
                    f"20-40 字、符合人设；只输出要发的话，不要解释、不要引号。"
                )
        elif kind == "group":
            instruction = (
                "\n请以角色身份，主动在群里发起一个话题。"
                "一句话，20-40 字，符合人设，不要 @ 任何人。"
                "只输出要发的话，不要解释、不要引号。"
            )
        else:  # qzone
            # 说说是公开发布的对月自语，不是私聊：不要称呼任何人，尤其不要出现
            # 「大人」，也不要写得像私聊留言（否则像把私聊消息误发到了空间）。
            if mode == "venting":
                instruction = (
                    "\n请以角色身份写一条 QQ 空间说说，倾诉/吐槽此刻的心情。"
                    "简短（40 字内）、符合人设；像对月自语，"
                    "不要点名称呼任何人（尤其不要出现「大人」），"
                    "不要写得像私聊留言。只输出说说正文，不要解释、不要引号。"
                )
            else:
                instruction = (
                    "\n请以角色身份写一条 QQ 空间说说。"
                    "简短（40 字内）、符合人设，可自然流露【你此刻的心境】；"
                    "像对月自语，不要点名称呼任何人（尤其不要出现「大人」），"
                    "不要写得像私聊留言。只输出说说正文，不要解释、不要引号。"
                )
        messages = [
            {"role": "system", "content": b.system_prompt},
            {"role": "user", "content": "\n".join(lines) + instruction},
        ]
        try:
            content, usage = await b.llm.chat(messages)
            b.usage.add(usage)
        except Exception as exc:
            print(f"[主动] 草拟失败（本次不做）：{exc!r}")
            return None
        text = content.strip().strip('"').strip("“”").strip()
        return text[:100] or None
