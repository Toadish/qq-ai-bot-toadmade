# -*- coding: utf-8 -*-
"""
test_channel_rpc.py — Channel API 调用（echo 关联）离线测试
============================================================
运行：..\..\.venv\Scripts\python.exe test_channel_rpc.py

覆盖：反向/正向通道的 call() 往返（echo 关联、响应不转发为事件）、
      无连接抛错、非响应报文原样放行。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # websocket 目录

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from channel import ChannelError, WsClientChannel, WsServerChannel  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        pass


async def t_server_call_roundtrip():
    ch = WsServerChannel()
    ws = FakeWS()
    ch._connections.add(ws)
    task = asyncio.create_task(ch.call("get_forward_msg", {"message_id": "x"}))
    await asyncio.sleep(0.05)
    req = json.loads(ws.sent[0])
    assert req["action"] == "get_forward_msg" and req["params"]["message_id"] == "x"
    assert req["echo"].startswith("rpc-")
    handled = ch._resolve_pending(json.dumps({
        "status": "ok", "retcode": 0, "data": {"messages": []}, "echo": req["echo"],
    }, ensure_ascii=False))
    assert handled is True
    resp = await asyncio.wait_for(task, 2)
    assert resp["retcode"] == 0
    print("PASS 反向通道 RPC：echo 关联往返、响应不再当事件转发")


async def t_client_call_roundtrip():
    ch = WsClientChannel("ws://127.0.0.1:1")
    ws = FakeWS()
    ch._ws = ws
    task = asyncio.create_task(ch.call("get_forward_msg", {"message_id": "y"}))
    await asyncio.sleep(0.05)
    req = json.loads(ws.sent[0])
    ch._resolve_pending(json.dumps({
        "status": "ok", "retcode": 0, "data": {}, "echo": req["echo"],
    }, ensure_ascii=False))
    resp = await asyncio.wait_for(task, 2)
    assert resp["status"] == "ok"
    print("PASS 正向通道 RPC：echo 关联往返")


async def t_no_connection_raises():
    ch = WsServerChannel()
    try:
        await ch.call("get_forward_msg", {})
        raise AssertionError("无连接时应抛 ChannelError")
    except ChannelError:
        pass
    print("PASS 无连接：call 抛 ChannelError")


async def t_unmatched_passthrough():
    ch = WsServerChannel()
    assert ch._resolve_pending(json.dumps({"post_type": "message"})) is False
    assert ch._resolve_pending("不是 JSON") is False
    print("PASS 非响应报文：原样放行")


async def t_response_resolves_while_hook_busy():
    # 回归：on_message 被一条慢消息占住（如大脑在等 RPC）时，响应仍能送达——
    # 收/处理已解耦（接收只入队，派发任务单独串行处理）。
    ch = WsServerChannel()
    ws = FakeWS()
    ch._connections.add(ws)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(raw):
        started.set()
        await release.wait()

    ch.on_message = slow_hook
    disp = asyncio.create_task(ch._dispatch())
    await ch._msg_queue.put("慢事件A")
    await asyncio.wait_for(started.wait(), 2)   # 派发器正被慢事件占住
    task = asyncio.create_task(ch.call("get_forward_msg", {"message_id": "x"}))
    await asyncio.sleep(0.05)
    req = json.loads(ws.sent[0])
    # 响应此刻到达：与派发器无关，直接完成 Future
    assert ch._resolve_pending(json.dumps({
        "status": "ok", "retcode": 0, "data": {}, "echo": req["echo"],
    }, ensure_ascii=False))
    resp = await asyncio.wait_for(task, 2)
    assert resp["status"] == "ok"
    release.set()
    disp.cancel()
    print("PASS 收/处理解耦：处理钩子忙时 API 响应不被堵死（死锁回归）")


if __name__ == "__main__":
    asyncio.run(t_server_call_roundtrip())
    asyncio.run(t_client_call_roundtrip())
    asyncio.run(t_no_connection_raises())
    asyncio.run(t_unmatched_passthrough())
    asyncio.run(t_response_resolves_while_hook_busy())
    print("\n全部测试通过 ✅")
