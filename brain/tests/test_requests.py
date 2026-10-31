# -*- coding: utf-8 -*-
"""
test_requests.py — 入群/加好友请求处理离线测试（假 LLM，不需要 API Key）
=========================================================================
运行：..\..\websocket\.venv\Scripts\python.exe test_requests.py

覆盖：请求事件解析（friend / group_add / group_invite）、同意/拒绝动作构造、
     mode=off / approve_all / refuse_all / llm 四种策略、决策失败保持待处理、
     格式异常不擅自拒绝、类型开关、请求不进入短期记忆。
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from brain.brain import ChatBrain, REQUEST_REFUSE_LINE  # noqa: E402
from brain.llm import LLMError  # noqa: E402
from brain.onebot import (build_set_request_action,  # noqa: E402
                          parse_request_event)
from memory.memory import MemoryStore  # noqa: E402


class FakeLLM:
    """可编排的假 LLM：按 next_chat 决定决策结果；calls 记录调用次数。"""

    def __init__(self, next_chat=None):
        self.next_chat = next_chat
        self.calls = []

    async def chat(self, messages, response_format=None, max_tokens=None):
        self.calls.append(messages)
        if callable(self.next_chat):
            result = self.next_chat()
        else:
            result = self.next_chat
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        pass


class FakeTimesource:
    """假时间源：固定北京时间，不发网络请求。"""

    def __init__(self):
        from datetime import datetime
        self.dt = datetime(2026, 10, 29, 16, 30)

    def last_known(self):
        return self.dt

    def observe_event_time(self, ts):
        pass

    async def refresh(self):
        pass

    async def close(self):
        pass


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.sender = self

    async def send(self, message):
        self.sent.append(message)


def t_parse():
    friend = parse_request_event({
        "post_type": "request", "request_type": "friend",
        "user_id": 111, "comment": "我是旧友", "flag": "f1",
    })
    assert friend["kind"] == "friend" and friend["flag"] == "f1"
    add = parse_request_event({
        "post_type": "request", "request_type": "group", "sub_type": "add",
        "user_id": 222, "group_id": 333, "comment": "求收留", "flag": "g1",
    })
    assert add["kind"] == "group_add" and add["sub_type"] == "add"
    invite = parse_request_event({
        "post_type": "request", "request_type": "group", "sub_type": "invite",
        "user_id": 444, "group_id": 555, "flag": "g2",
    })
    assert invite["kind"] == "group_invite" and invite["comment"] == ""
    assert parse_request_event({"post_type": "message"}) is None
    assert parse_request_event({
        "post_type": "request", "request_type": "group", "sub_type": "kick",
    }) is None
    print("PASS 请求事件解析：三类请求 + 不支持类型返回 None")


def t_build():
    friend = {"kind": "friend", "flag": "f1"}
    a1 = build_set_request_action(friend, True)
    assert a1["action"] == "set_friend_add_request" and a1["params"]["approve"] is True
    a2 = build_set_request_action(friend, False, "不太熟")
    assert a2["params"]["approve"] is False and "reason" not in a2["params"]
    group = {"kind": "group_add", "sub_type": "add", "flag": "g1"}
    a3 = build_set_request_action(group, False, "非故人")
    assert a3["action"] == "set_group_add_request"
    assert a3["params"]["sub_type"] == "add" and a3["params"]["reason"] == "非故人"
    a4 = build_set_request_action(group, True, "随便")
    assert a4["params"]["approve"] is True and "reason" not in a4["params"]
    print("PASS 请求动作构造：同意/拒绝、群理由字段、好友无理由字段")


def _make_brain(mode, switches=None, fake_llm=None, store=None):
    """布置临时工作区并返回 (brain, channel, log_dir)。"""
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text(
        "你是妖刀姬。避世修心，只与在意的人来往。", encoding="utf-8"
    )
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬"}), encoding="utf-8"
    )
    req = {"mode": mode, "friend": True, "group_add": True, "group_invite": True}
    if switches:
        req.update(switches)
    cfg = {
        "persona": {"slug": "yotohime"},
        "brain": {"collect_received_images": False},
        "requests": req,
        "short_term": {"window_turns": 12, "summary_chunk_turns": 10,
                       "max_level1_summaries": 5, "level2_chunk_summaries": 4,
                       "max_level2_summaries": 3, "summary_max_chars": 300,
                       "context_budget_chars": 4000},
        "llm": {"daily_budget_tokens": 100000, "warn_ratio": 0.8},
    }
    channel = FakeChannel()
    brain = ChatBrain(
        channel, cfg, config_dir=ws_dir, store=store,
        llm=fake_llm or FakeLLM(), timesource=FakeTimesource(),
    )
    return brain, channel, persona_dir.parent.parent / "personas" / "yotohime" / "sessions"


def _event(kind):
    if kind == "friend":
        return {"post_type": "request", "request_type": "friend",
                "user_id": 111, "comment": "故人来访", "flag": "f1"}
    if kind == "group_add":
        return {"post_type": "request", "request_type": "group", "sub_type": "add",
                "user_id": 222, "group_id": 333, "comment": "求收留", "flag": "g1"}
    return {"post_type": "request", "request_type": "group", "sub_type": "invite",
            "user_id": 444, "group_id": 555, "flag": "g2"}


async def _run_once(brain, channel, kind):
    await brain.handle_event(json.dumps(_event(kind), ensure_ascii=False))
    return [json.loads(s) for s in channel.sent]


async def t_modes():
    # llm 决策：同意
    brain, channel, _ = _make_brain("llm", fake_llm=FakeLLM(
        lambda: (json.dumps({"approve": True, "reason": ""}), {"total_tokens": 10})
    ))
    await brain.startup()
    acts = await _run_once(brain, channel, "friend")
    assert len(acts) == 1 and acts[0]["action"] == "set_friend_add_request"
    assert acts[0]["params"]["approve"] is True
    assert brain.usage.data["used"] > 0  # 计入话费

    # llm 决策：拒绝（群，带理由）
    brain, channel, _ = _make_brain("llm", fake_llm=FakeLLM(
        lambda: (json.dumps({"approve": False, "reason": "不相识"}), {"total_tokens": 10})
    ))
    await brain.startup()
    acts = await _run_once(brain, channel, "group_add")
    assert len(acts) == 1 and acts[0]["params"]["approve"] is False
    assert acts[0]["params"]["reason"] == "不相识"

    # llm 决策失败（网络异常）→ 不发任何动作，请求保持待处理
    brain, channel, _ = _make_brain("llm", fake_llm=FakeLLM(LLMError("网络断了")))
    await brain.startup()
    acts = await _run_once(brain, channel, "group_invite")
    assert acts == []

    # llm 输出缺 approve 字段 → 视为失败，不擅自拒绝
    brain, channel, _ = _make_brain("llm", fake_llm=FakeLLM(
        lambda: (json.dumps({"reply": True, "text": "你好"}), {"total_tokens": 10})
    ))
    await brain.startup()
    acts = await _run_once(brain, channel, "friend")
    assert acts == []

    # approve_all / refuse_all / off
    brain, channel, _ = _make_brain("approve_all", fake_llm=FakeLLM())
    await brain.startup()
    acts = await _run_once(brain, channel, "friend")
    assert acts[0]["params"]["approve"] is True and brain.llm.calls == []

    brain, channel, _ = _make_brain("refuse_all", fake_llm=FakeLLM())
    await brain.startup()
    acts = await _run_once(brain, channel, "group_add")
    assert acts[0]["params"]["approve"] is False
    assert acts[0]["params"]["reason"] == REQUEST_REFUSE_LINE

    brain, channel, _ = _make_brain("off", fake_llm=FakeLLM())
    await brain.startup()
    acts = await _run_once(brain, channel, "friend")
    assert acts == [] and brain.llm.calls == []
    print("PASS 四种 mode：llm 同意/拒绝/失败待处理、格式异常不拒绝、全同意/全拒绝/仅记录")


async def t_switches_and_stm():
    # 关掉 friend 开关：好友申请静默忽略
    brain, channel, _ = _make_brain("approve_all", switches={"friend": False})
    await brain.startup()
    acts = await _run_once(brain, channel, "friend")
    assert acts == []

    # 请求事件不进入短期记忆（不是聊天），但会留一条"系统"轮次让主大脑知道
    brain, channel, _ = _make_brain("approve_all")
    await brain.startup()
    await _run_once(brain, channel, "group_add")
    assert len(brain.stm.window) == 1
    assert brain.stm.window[0]["role"] == "system"
    assert "入群申请" in brain.stm.window[0]["text"]
    print("PASS 类型开关与短期记忆隔离：请求不进聊天窗口，但留系统轮次告知大脑")


async def t_logging():
    brain, channel, log_dir = _make_brain("llm", fake_llm=FakeLLM(
        lambda: (json.dumps({"approve": False, "reason": "不相识"}), {"total_tokens": 10})
    ))
    await brain.startup()
    await _run_once(brain, channel, "group_add")
    await brain.shutdown()
    today = log_dir / f"{datetime.now():%Y%m%d}.log"
    assert today.exists() and "入群申请" in today.read_text(encoding="utf-8")
    assert "拒绝" in today.read_text(encoding="utf-8")
    print("PASS 日志落盘：请求与处置结果写入会话日志")


async def t_group_channel_on_approve():
    # 同意入群申请/邀请 → 建立群发送通道（主动事件渠道清单，长期记忆）
    store = MemoryStore(Path(tempfile.mkdtemp(prefix="req_test_")) / "memory.json",
                        seed=1, clock=FakeTimesource().last_known)
    brain, channel, _ = _make_brain("approve_all", store=store)
    await brain.startup()
    await _run_once(brain, channel, "group_add")
    assert "333" in store.channel_lists()[0], "同意入群申请应建立群通道"
    await _run_once(brain, channel, "group_invite")
    assert "555" in store.channel_lists()[0], "同意入群邀请也应建立群通道"
    await brain.shutdown()
    print("PASS 同意入群申请/邀请：建立群发送通道（长期记忆持久化）")


if __name__ == "__main__":
    t_parse()
    t_build()
    asyncio.run(t_modes())
    asyncio.run(t_switches_and_stm())
    asyncio.run(t_logging())
    asyncio.run(t_group_channel_on_approve())
    print("\n全部测试通过 ✅")
