# -*- coding: utf-8 -*-
"""
test_brain.py — 聊天大脑离线测试（假 LLM，不需要 API Key）
============================================================
运行：..\..\websocket\.venv\Scripts\python.exe test_brain.py

覆盖：OneBot 解析/动作构造、表情库（关键词命中/收藏去重）、
      "话费"记账（阈值触发当日一次）、大脑全流程（决策→回复→日志→
      下机总结→人物更新）。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from brain.brain import ChatBrain          # noqa: E402
from brain.emoji import EmojiLibrary       # noqa: E402
from brain.onebot import (build_send_action, forward_nodes_to_text,  # noqa: E402
                          build_send_qzone_action, segments_to_text)
from brain.usage import TokenUsage         # noqa: E402
from memory.memory import MemoryStore      # noqa: E402


class FakeLLM:
    """假 DeepSeek 客户端：按指令内容区分三种调用（决策/压缩/总结）。"""

    def __init__(self):
        self.calls = []

    async def chat(self, messages, response_format=None, max_tokens=None):
        user = messages[-1]["content"] if messages else ""
        self.calls.append(user)
        if "压缩" in user:
            return "[摘要] 压缩结果", {"total_tokens": 100}
        if "回顾" in user or "完整记录" in user:
            return (
                "- 一起看了月亮\n"
                "- 大人提到盘缠不够\n"
                "人物更新 | 111 | 大人 | 加标签:阔绰 | 好感度变化:+1",
                {"total_tokens": 200},
            )
        return json.dumps(
            {"reply": True, "text": "嗯。", "emoji": "开心", "mentioned": ["1"],
             "mood_delta": {"怒": 2}, "global_mood_delta": {"哀": 1}},
            ensure_ascii=False,
        ), {"total_tokens": 150}

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
        self.call_log = []      # (action, params)
        self.call_result = None

    async def send(self, message):
        self.sent.append(message)

    async def call(self, action, params, timeout=10.0):
        self.call_log.append((action, params))
        return self.call_result


def t_onebot():
    event = {
        "post_type": "message", "message_type": "group",
        "user_id": 111, "group_id": 222,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 你好"}},
            {"type": "image", "data": {"file": "/tmp/x.png"}},
        ],
    }
    assert segments_to_text(event["message"], self_id=999) == "@我 你好[图片]"
    # 默认（回复性消息）：群聊不带 @，像真人接话
    action = build_send_action(event, [{"type": "text", "data": {"text": "嗯"}}])
    assert action["params"]["message"][0]["type"] == "text"
    # mention=True（预留给主动发起话题）：@ 消息发送者
    action2 = build_send_action(
        event, [{"type": "text", "data": {"text": "嗯"}}], mention=True
    )
    assert action2["params"]["message"][0]["type"] == "at"
    assert action2["params"]["message"][0]["data"]["qq"] == "111"
    print("PASS onebot 解析与动作构造（群聊回复不带@，mention 时@）")


def t_qzone_action():
    a = build_send_qzone_action("今晚的月亮……很好。", images=["x.png"], ugc_right=64)
    assert a["action"] == "send_qzone_msg"
    assert a["params"]["content"] == "今晚的月亮……很好。"
    assert a["params"]["ugc_right"] == 64
    assert a["params"]["images"] == ["x.png"]
    b = build_send_qzone_action("默认权限")
    assert b["params"]["ugc_right"] == 1 and "images" not in b["params"]
    print("PASS 说说动作构造：send_qzone_msg 内容/权限/图片参数")


def t_forward_text():
    nodes = [
        {"sender": {"nickname": "甲"}, "content": [
            {"type": "text", "data": {"text": "今天月亮真圆"}},
            {"type": "image", "data": {"file": "x.png"}},
        ]},
        {"type": "node", "data": {"name": "乙", "message": "转发里也能看到"},
         },
    ]
    out = forward_nodes_to_text(nodes)
    assert "甲：今天月亮真圆[图片]" in out
    assert "乙：转发里也能看到" in out
    # 截断保护
    long_nodes = [{"sender": {"nickname": "丙"}, "content": "很" * 2000}]
    cut = forward_nodes_to_text(long_nodes, max_chars=800)
    assert len(cut) <= 803 and cut.endswith("……")
    print("PASS 合并转发解析：节点转文本、嵌套形态兼容、超长截断")


def t_emoji():
    tmp = Path(tempfile.mkdtemp())
    lib = EmojiLibrary(tmp, min_interval_messages=1)
    (tmp / "emoji" / "猫猫-开心.png").write_bytes(b"img1")
    (tmp / "emoji" / "晚安.txt").write_bytes(b"not image")
    lib.refresh()
    kws = lib.keywords()
    assert "开心" in kws and "猫猫" in kws
    # LLM 情绪联想通道：按关键词挑表情
    kw_pick = lib.pick_keyword("开心")
    assert kw_pick is not None and kw_pick.name == "猫猫-开心.png"
    assert lib.pick_keyword("不存在") is None   # 未知关键词：返回 None、不消耗门控
    assert lib.pick_keyword("猫猫") is None     # 冷却门控：刚挑过一张，同 30 秒内被拒
    # 文本关键词兜底通道（新实例，避开冷却）
    lib2 = EmojiLibrary(tmp, min_interval_messages=1)
    pick = lib2.pick("今天很开心！")
    assert pick is not None and pick.name == "猫猫-开心.png"
    src = tmp / "src.png"
    src.write_bytes(b"data")
    c1 = lib2.collect_image(src)
    c2 = lib2.collect_image(src)
    assert c1 == c2 and len(lib2._collected) == 1
    b1 = lib2.collect_bytes(b"bytes-img", ".png")
    b2 = lib2.collect_bytes(b"bytes-img", ".png")   # 字节收藏同样哈希去重
    assert b1 == b2 and len(lib2._collected) == 2
    assert b1.read_bytes() == b"bytes-img"
    print("PASS 表情库：文本命中、LLM 联想、冷却门控、本地/字节收藏去重")


def t_usage():
    path = Path(tempfile.mkdtemp()) / "usage.json"
    u = TokenUsage(path, daily_budget_tokens=100, warn_ratio=0.8)
    assert u.warning_pending() is False
    u.add({"total_tokens": 85})
    assert u.warning_pending() is True    # 首次触发
    assert u.warning_pending() is False   # 当日只提醒一次
    print("PASS 话费记账：阈值触发且当日仅一次")


async def t_brain_flow():
    # 按生产约定布置临时工作区：根 = tmp，config 目录 = tmp/websocket，
    # 人设 = tmp/personas/<slug>/（brain 用 config_dir 的父级定位一切）
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text(
        "你是妖刀姬。短句，句尾省略号。", encoding="utf-8"
    )
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬", "mood_calm_factor": 0.5,
                    "group_silence_chance": 0}),   # 全流程测试固定不沉默（确定性）
        encoding="utf-8"
    )
    cfg = {
        "persona": {"slug": "yotohime", "owner_qq": "111"},
        "brain": {"max_reply_chars": 300, "collect_received_images": True,
                  "emoji_min_interval_messages": 4},
        "short_term": {"window_turns": 12, "summary_chunk_turns": 10,
                       "max_level1_summaries": 5, "level2_chunk_summaries": 4,
                       "max_level2_summaries": 3, "summary_max_chars": 300,
                       "context_budget_chars": 4000},
        "llm": {"daily_budget_tokens": 100000, "warn_ratio": 0.8},
    }
    ts = FakeTimesource()
    store = MemoryStore(tmp / "memory.json", seed=1, clock=ts.last_known,
                        params={"ordinary_forget_rate": 0.0})
    store.session_end(["一起看过雨"])   # 先种一条回忆（编号将为 [1]）
    from datetime import timedelta
    ts.dt += timedelta(days=1)          # 次日上机：每日仪式会正常执行（count 0→1）
    channel = FakeChannel()
    fake_llm = FakeLLM()
    brain = ChatBrain(channel, cfg, config_dir=ws_dir, store=store,
                      llm=fake_llm, timesource=ts)
    await brain.startup()
    assert "【大人】QQ 号是 111" in brain.system_prompt  # owner_qq 注入
    assert brain._mention_map.get("1"), "种下的回忆应有会话内编号 [1]"
    event = {
        "post_type": "message", "message_type": "group",
        "user_id": 111, "group_id": 222,
        "sender": {"nickname": "某人"},
        "message": [{"type": "text", "data": {"text": "今晚月亮好圆"}}],
    }
    await brain.handle_event(json.dumps(event, ensure_ascii=False))
    assert channel.sent, "应发出回复"
    action = json.loads(channel.sent[0])
    assert action["action"] == "send_msg"
    assert action["params"]["message"][0]["type"] == "text"  # 回复不带 @
    assert brain._emoji_hint == ""                            # 空表情库：零注入成本
    assert all(s.get("type") != "image" for s in action["params"]["message"]), \
        "空表情库不应附图片（LLM 给了 emoji 字段也要兜底为空）"
    # 提起加分：每日仪式 count 0→1，LLM 报 [1] 再加 1 → 2
    assert store.data["ordinary"][0]["count"] == 2
    # 情绪两级：心境注入 + 按人情绪注入 + 落账（怒 5+2=7；哀 10+1=11）
    assert "【你此刻的心境】" in fake_llm.calls[0]
    assert "【你此刻对他的情绪】" in fake_llm.calls[0]
    assert brain.mood.calm_factor == 0.5     # 人设 meta 的平复因子生效
    assert brain.mood.snapshot()["111"]["mood"]["怒"] == 7
    assert brain.mood.snapshot()["_global"]["mood"]["哀"] == 11
    # 时间可用 → 每次决策都附带一行【当前时间】（用不用由 LLM 自行判断）
    assert "【当前时间】" in fake_llm.calls[0]
    # 与时间无关的消息同样附带（不靠关键词触发，冷门时间词也不漏判）
    # 群聊两步决策：calls[1] 是第一条消息的回复调用，calls[2] 才是第二条的决策
    event2 = dict(event)
    event2["message"] = [{"type": "text", "data": {"text": "刀出鞘了吗"}}]
    await brain.handle_event(json.dumps(event2, ensure_ascii=False))
    assert "【当前时间】" in fake_llm.calls[2]
    # 时间不可用 → 整行省略（不给 LLM 错误参考）；calls[4] 是第三条的决策调用
    ts.dt = None
    event3 = dict(event)
    event3["message"] = [{"type": "text", "data": {"text": "冷不冷"}}]
    await brain.handle_event(json.dumps(event3, ensure_ascii=False))
    assert "【当前时间】" not in fake_llm.calls[4]
    # 同一天再次报 [1]：限次生效，计数不再涨
    assert store.data["ordinary"][0]["count"] == 2
    # 收图：file:// URI 也归一化入库
    img_src = tmp / "a.png"
    img_src.write_bytes(b"png-data")
    event4 = dict(event)
    event4["message"] = [
        {"type": "image", "data": {"file": "file:///" + str(img_src).replace(chr(92), "/")}}
    ]
    await brain.handle_event(json.dumps(event4, ensure_ascii=False))
    collected = list((ws_dir.parent / "images" / "collected").iterdir())
    assert len(collected) == 1, f"收到的图片应入库 1 张，实际 {len(collected)}"
    # 说说能力层（未接线）：直接调用发布 + 空内容拒绝
    assert await brain.publish_qzone("今晚的月亮……很好。")
    qzones = [json.loads(s) for s in channel.sent
              if json.loads(s).get("action") == "send_qzone_msg"]
    assert len(qzones) == 1
    assert qzones[0]["params"]["content"] == "今晚的月亮……很好。"
    assert not await brain.publish_qzone("   ")   # 空内容：不发送
    assert len(store.data["ordinary"]) == 1  # 尚未下机总结（只有种下的那条）
    await brain.shutdown()
    texts = [m["text"] for m in store.data["ordinary"]]
    assert "一起看了月亮" in texts
    assert store.data["people"]["111"]["favor"] == 1  # 日志里真实出现过的 QQ 才被接受
    # 下机收尾：分情绪牵引长期情绪 + 向基线回落（首次：怒 13 → 13+0.05×(5-13)=12.6）
    assert abs(store.data["people"]["111"]["mood"]["怒"] - 12.6) < 0.01
    assert brain.mood.snapshot()["_state"]["online"] is False  # 已切离线
    print("PASS 大脑全流程：决策→回复→日志→下机总结→人物更新（时间/表情/提起加分/情绪）")


async def t_proactive_activity_gate():
    # 框架噪音（心跳/通知）不应重置空闲计时；只有真正的聊天消息才算活动
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text("你是妖刀姬。", encoding="utf-8")
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬"}), encoding="utf-8"
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
        "proactive": {"enabled": True, "tick_seconds": 3600},
    }
    ts = FakeTimesource()
    brain = ChatBrain(FakeChannel(), cfg, config_dir=ws_dir, store=None,
                      llm=FakeLLM(), timesource=ts)
    await brain.startup()
    before = brain.proactive._last_activity
    # 心跳 meta_event：不算活动
    await brain.handle_event(json.dumps({
        "post_type": "meta_event", "meta_event_type": "heartbeat",
        "time": int(ts.dt.timestamp()), "self_id": 999,
    }, ensure_ascii=False))
    assert brain.proactive._last_activity == before
    # 通知（输入状态等）：不算活动
    await brain.handle_event(json.dumps({
        "post_type": "notice", "notice_type": "notify",
        "time": int(ts.dt.timestamp()), "self_id": 999,
    }, ensure_ascii=False))
    assert brain.proactive._last_activity == before
    # 真正的聊天消息：算活动
    await brain.handle_event(json.dumps({
        "post_type": "message", "message_type": "private",
        "user_id": 111, "time": int(ts.dt.timestamp()),
        "message": [{"type": "text", "data": {"text": "在吗"}}],
    }, ensure_ascii=False))
    assert brain.proactive._last_activity > before
    await brain.shutdown()
    print("PASS 活动判定：心跳/通知不重置空闲计时，聊天消息才重置")


async def t_forward_reading():
    # 合并转发：inline content 优先，否则走 get_forward_msg RPC
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text("你是妖刀姬。", encoding="utf-8")
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬"}), encoding="utf-8"
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
        "proactive": {"enabled": True, "tick_seconds": 3600},
    }
    channel = FakeChannel()
    brain = ChatBrain(channel, cfg, config_dir=ws_dir, store=None,
                      llm=FakeLLM(), timesource=FakeTimesource())
    await brain.startup()
    # 路径 1：inline content（NapCat 数组格式）
    await brain.handle_event(json.dumps({
        "post_type": "message", "message_type": "private", "user_id": 111,
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "forward", "data": {"content": [
                {"sender": {"nickname": "甲"}, "content": [
                    {"type": "text", "data": {"text": "转发的第一句"}}]},
            ]}},
        ],
    }, ensure_ascii=False))
    last = brain.llm.calls[-1]
    assert "【合并转发】" in last and "转发的第一句" in last
    assert not channel.call_log, "有 inline content 时不应走 RPC"
    # 路径 2：只有 id → RPC 拉取
    channel.call_result = {"status": "ok", "retcode": 0, "data": {"messages": [
        {"sender": {"nickname": "乙"}, "content": [
            {"type": "text", "data": {"text": "RPC 拉到的内容"}}]},
    ]}}
    await brain.handle_event(json.dumps({
        "post_type": "message", "message_type": "private", "user_id": 111,
        "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
    }, ensure_ascii=False))
    assert any(a == "get_forward_msg" for a, p in channel.call_log)
    assert "RPC 拉到的内容" in brain.llm.calls[-1]
    await brain.shutdown()
    print("PASS 合并转发阅读：inline 优先 + get_forward_msg RPC 兜底")


class GroupFakeLLM:
    """群聊两步决策专用：按顺序先出「想回」决策 JSON，再出回复 JSON。"""

    def __init__(self, decision="", reply=None):
        self.decision = decision
        self.reply = reply
        self.calls = []

    async def chat(self, messages, response_format=None, max_tokens=None):
        user = messages[-1]["content"] if messages else ""
        self.calls.append(user)
        if len(self.calls) == 1:
            return self.decision, {"total_tokens": 10}
        if self.reply is None:
            raise AssertionError("被沉默时不应再发起回复调用")
        return self.reply, {"total_tokens": 10}

    async def close(self):
        pass


def _group_setup(silence=0.0, blacklist=()):
    tmp = Path(tempfile.mkdtemp())
    ws_dir = tmp / "websocket"
    ws_dir.mkdir()
    persona_dir = tmp / "personas" / "yotohime"
    persona_dir.mkdir(parents=True)
    (persona_dir / "system_prompt.md").write_text("你是妖刀姬。", encoding="utf-8")
    (persona_dir / "meta.json").write_text(
        json.dumps({"display_name": "妖刀姬", "group_silence_chance": silence}),
        encoding="utf-8"
    )
    cfg = {
        "persona": {"slug": "yotohime", "owner_qq": "111"},
        "brain": {"collect_received_images": False,
                  "group_reply_blacklist": list(blacklist)},
        "requests": {"mode": "off", "friend": True, "group_add": True, "group_invite": True},
        "short_term": {"window_turns": 12, "summary_chunk_turns": 10,
                       "max_level1_summaries": 5, "level2_chunk_summaries": 4,
                       "max_level2_summaries": 3, "summary_max_chars": 300,
                       "context_budget_chars": 4000},
        "llm": {"daily_budget_tokens": 100000, "warn_ratio": 0.8},
        "mood": {},
        "proactive": {"enabled": True, "tick_seconds": 3600},
    }
    return ws_dir, cfg


def _group_event(user_id=111, group_id=222, text="刀在吗"):
    return json.dumps({
        "post_type": "message", "message_type": "group",
        "group_id": group_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }, ensure_ascii=False)


async def t_group_silence_flow():
    # 放行：两步调用 → 回复发出，情绪落账
    ws_dir, cfg = _group_setup(silence=0.0)
    channel = FakeChannel()
    llm = GroupFakeLLM(
        decision=json.dumps({"reply": True, "mood_delta": {"哀": 1}}, ensure_ascii=False),
        reply=json.dumps({"text": "……随你。", "emoji": ""}, ensure_ascii=False),
    )
    brain = ChatBrain(channel, cfg, config_dir=ws_dir, store=None,
                      llm=llm, timesource=FakeTimesource())
    await brain.startup()
    await brain.handle_event(_group_event())
    assert len(llm.calls) == 2, "群聊回复应走两步：决策 + 生成回复"
    acts = [json.loads(s) for s in channel.sent]
    assert acts and acts[0]["action"] == "send_msg" and acts[0]["params"]["message_type"] == "group"
    assert brain.mood.snapshot()["111"]["mood"]["哀"] == 11   # 决策里的情绪照常落账（对人的）
    await brain.shutdown()

    # 沉默因子生效：只有一次决策调用，不生成回复，情绪仍落账
    ws_dir, cfg = _group_setup(silence=1.0)
    channel2 = FakeChannel()
    llm2 = GroupFakeLLM(
        decision=json.dumps({"reply": True, "mood_delta": {"怒": 1}}, ensure_ascii=False)
    )
    brain2 = ChatBrain(channel2, cfg, config_dir=ws_dir, store=None,
                       llm=llm2, timesource=FakeTimesource())
    await brain2.startup()
    await brain2.handle_event(_group_event())
    assert len(llm2.calls) == 1, "被沉默时只应有决策调用，不生成回复"
    assert channel2.sent == []
    assert brain2.mood.snapshot()["111"]["mood"]["怒"] == 6   # 被沉默情绪也落账（对人的）
    await brain2.shutdown()

    # 黑名单：零调用零动作
    ws_dir, cfg = _group_setup(silence=0.0, blacklist=("999",))
    channel3 = FakeChannel()
    llm3 = GroupFakeLLM(decision="{}")
    brain3 = ChatBrain(channel3, cfg, config_dir=ws_dir, store=None,
                       llm=llm3, timesource=FakeTimesource())
    await brain3.startup()
    await brain3.handle_event(_group_event(user_id=999))
    assert llm3.calls == [] and channel3.sent == [], "黑名单消息应零调用零动作"
    await brain3.shutdown()
    print("PASS 群聊沉默回路：两步决策、沉默因子省 token、情绪照常落账、黑名单零调用")


if __name__ == "__main__":
    t_onebot()
    t_forward_text()
    t_qzone_action()
    t_emoji()
    t_usage()
    asyncio.run(t_brain_flow())
    asyncio.run(t_proactive_activity_gate())
    asyncio.run(t_forward_reading())
    asyncio.run(t_group_silence_flow())
    print("\n全部测试通过 ✅")
