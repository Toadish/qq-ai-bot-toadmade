# -*- coding: utf-8 -*-
"""
test_proactive.py — 主动发起话题 / 发朋友圈事件离线测试
=========================================================
运行：..\..\websocket\.venv\Scripts\python.exe test_proactive.py

覆盖：无聊事件（喜哀低 + 空闲 → 随机因子 → 群/私聊/说说三选一、冷却计入每次尝试）、
      倾诉事件（心境 >60 → 必定行动、私聊/说说二选一、长冷却）、
      倾诉优先于无聊、群聊不带 @。
"""

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from brain import proactive as pm                       # noqa: E402
from brain.brain import ChatBrain                       # noqa: E402
from memory.memory import MemoryStore                   # noqa: E402


class FakeLLM:
    """决策返回不回；草拟返回固定文案。"""

    def __init__(self, draft="……想和谁说说话。"):
        self.draft = draft
        self.calls = []

    async def chat(self, messages, response_format=None, max_tokens=None):
        user = messages[-1]["content"]
        self.calls.append(user[:80])
        if "请以角色身份判断要不要回复" in user:
            return json.dumps({"reply": False}, ensure_ascii=False), {"total_tokens": 10}
        return self.draft, {"total_tokens": 10}

    async def close(self):
        pass


class FakeTimesource:
    def __init__(self):
        self.dt = datetime(2026, 10, 30, 10, 0)
        self.expired = False      # True = 缓存过期（last_known 返回 None）
        self.refreshed = 0

    def last_known(self):
        return None if self.expired else self.dt

    def observe_event_time(self, ts):
        pass

    async def refresh(self):
        self.refreshed += 1
        self.expired = False

    async def close(self):
        pass


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.sender = self
        self.connected = True

    async def send(self, message):
        self.sent.append(message)


def _make(people=None, chance=1.0):
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text(
        "你是妖刀姬。避世修心，只与在意的人来往。", encoding="utf-8"
    )
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬", "mood_calm_factor": 1.0,
                    "boredom_chance": chance}),
        encoding="utf-8"
    )
    cfg = {
        "persona": {"slug": "yotohime", "owner_qq": "111"},
        "brain": {"collect_received_images": False},
        "requests": {"mode": "off", "friend": True, "group_add": True, "group_invite": True},
        "short_term": {"window_turns": 12, "summary_chunk_turns": 10,
                       "max_level1_summaries": 5, "level2_chunk_summaries": 4,
                       "max_level2_summaries": 3, "summary_max_chars": 300,
                       "context_budget_chars": 4000},
        "llm": {"daily_budget_tokens": 100000, "warn_ratio": 0.8},
        "mood": {},
        "proactive": {
            "enabled": True, "tick_seconds": 3600, "idle_seconds": 300,
            "boredom_cooldown_seconds": 300, "boredom_chance": chance,
            "boredom_joy_max": 20, "boredom_sorrow_max": 20,
            "vent_threshold": 60, "vent_cooldown_seconds": 10800,
        },
    }
    ts = FakeTimesource()
    store = MemoryStore(tmp / "memory.json", seed=1, clock=ts.last_known)
    if people:
        store.data["people"] = people
    channel = FakeChannel()
    brain = ChatBrain(channel, cfg, config_dir=ws_dir, store=store,
                      llm=FakeLLM(), timesource=ts)
    return brain, channel


async def _idle(brain):
    brain.proactive._last_activity = time.monotonic() - 400


def _qzone_actions(channel):
    return [s for s in channel.sent if json.loads(s).get("action") == "send_qzone_msg"]


async def t_boredom_qzone_and_cooldown():
    brain, channel = _make(people={}, chance=1.0)
    await brain.startup()
    await _idle(brain)
    await brain.proactive._tick()
    assert len(_qzone_actions(channel)) == 1, "无群无人时应落到发朋友圈"
    await brain.proactive._tick()          # 冷却 5 分钟内：不再触发
    assert len(_qzone_actions(channel)) == 1
    await brain.proactive.stop()
    print("PASS 无聊事件：触发发朋友圈 + 冷却期内不重复")


async def t_boredom_roll_skip():
    brain, channel = _make(people={}, chance=0.0)
    await brain.startup()
    await _idle(brain)
    await brain.proactive._tick()
    assert channel.sent == [], "随机因子未选中：保持沉默"
    assert brain.mood.data["_state"].get("bored_at"), "尝试也计入冷却"
    await brain.proactive.stop()
    print("PASS 无聊随机因子：未选中跳过且计入冷却")


async def t_boredom_group_no_at():
    brain, channel = _make(people={"111": {"name": "甲", "labels": [], "favor": 0}},
                           chance=1.0)
    await brain.startup()
    brain.proactive.note_group("222")
    await _idle(brain)
    with mock.patch.object(pm.random, "choices", side_effect=lambda pop, weights, k: [pop[0]]), \
         mock.patch.object(pm.random, "choice", side_effect=lambda seq: seq[0]):
        await brain.proactive._tick()
    acts = [json.loads(s) for s in channel.sent]
    assert acts and acts[0]["action"] == "send_msg"
    assert acts[0]["params"]["message_type"] == "group"
    assert all(s.get("type") != "at" for s in acts[0]["params"]["message"]), \
        "群聊发起话题不得 @ 任何人"
    await brain.proactive.stop()
    print("PASS 无聊事件群聊：从通道清单随机已知群、不带 @")


async def t_venting_private():
    brain, channel = _make(people={"111": {"name": "甲", "labels": [], "favor": 0}},
                           chance=1.0)
    await brain.startup()
    for _ in range(12):
        brain.mood.apply_global_delta({"怒": 5})   # 怒 5 → 65 > 60
    await _idle(brain)
    with mock.patch.object(pm.random, "choices", side_effect=lambda pop, weights, k: [pop[0]]), \
         mock.patch.object(pm.random, "choice", side_effect=lambda seq: seq[0]):
        await brain.proactive._tick()
    acts = [json.loads(s) for s in channel.sent]
    assert acts and acts[0]["params"]["message_type"] == "private"
    assert acts[0]["params"]["user_id"] == "111"
    await brain.proactive._tick()          # 长冷却：不再触发
    assert len(channel.sent) == 1
    await brain.proactive.stop()
    print("PASS 倾诉事件：心境>60 必定行动、私聊在意的人、长冷却")


async def t_venting_priority_over_boredom():
    brain, channel = _make(people={"111": {"name": "甲", "labels": [], "favor": 0}},
                           chance=1.0)
    await brain.startup()
    for _ in range(12):
        brain.mood.apply_global_delta({"怒": 5})   # 怒高，同时喜哀低（无聊条件也成立）
    await _idle(brain)
    await brain.proactive._tick()
    assert len(channel.sent) == 1, "倾诉优先：本轮只应有一个动作"
    await brain.proactive.stop()
    print("PASS 倾诉优先：无聊条件同时成立时只触发倾诉")


async def t_tick_refreshes_timesource():
    # 长空闲后时间缓存过期（last_known=None）→ 主动循环应自行刷新再判定
    brain, channel = _make(people={}, chance=1.0)
    await brain.startup()
    brain.timesource.expired = True
    await _idle(brain)
    await brain.proactive._tick()
    assert brain.timesource.refreshed >= 1, "空闲期应刷新时间源"
    assert len(_qzone_actions(channel)) == 1, "刷新后应正常触发无聊事件"
    await brain.proactive.stop()
    print("PASS 空闲期刷新时间源：长挂机后主动事件不再被时间源挡住")


async def t_channel_registry_and_weights():
    # 通道清单在长期记忆（memory.json channels）：群通道/私聊通道 + 加权抽取
    brain, _ = _make(people={"111": {"name": "甲", "labels": [], "favor": 0}}, chance=0.0)
    await brain.startup()
    store = brain.store
    store.note_group_channel("222")
    store.note_private_channel("333")
    # 无聊：三渠道，权重 群1.0 / 私聊1.5 / 说说0.5
    names, weights, pool = brain.proactive._channel_pool("boredom")
    assert names == ["group", "private", "qzone"]
    assert weights == [1.0, 1.5, 0.5], f"无聊渠道权重应为 1.0/1.5/0.5：{weights}"
    assert pool[0] == ["222"]
    assert "111" in pool[1] and "333" in pool[1]   # 私聊候选 = 清单 ∪ 人物层
    # 倾诉：私聊/说说二选一，权重 1.5/0.5
    v_names, v_weights, _ = brain.proactive._channel_pool("venting")
    assert v_names == ["private", "qzone"] and v_weights == [1.5, 0.5]
    # 旧数据迁移：mood _state.known_groups → 群通道清单
    brain.mood.data.setdefault("_state", {})["known_groups"] = ["444"]
    fresh = pm.ProactiveAgent(brain)
    assert "444" in store.channel_lists()[0], "旧 known_groups 应迁移进通道清单"
    assert "known_groups" not in brain.mood.data.get("_state", {})
    await brain.proactive.stop()
    print("PASS 通道清单与权重：长期记忆渠道池、私聊1.5/说说0.5、旧数据迁移")


if __name__ == "__main__":
    asyncio.run(t_boredom_qzone_and_cooldown())
    asyncio.run(t_boredom_roll_skip())
    asyncio.run(t_boredom_group_no_at())
    asyncio.run(t_venting_private())
    asyncio.run(t_venting_priority_over_boredom())
    asyncio.run(t_tick_refreshes_timesource())
    asyncio.run(t_channel_registry_and_weights())
    print("\n全部测试通过 ✅")
