# -*- coding: utf-8 -*-
"""
最小 WebSocket 回显服务器（仅用于本地测试 client.py）
====================================================
用法：
    终端 A：python echo_server.py
    终端 B：python client.py -u ws://127.0.0.1:8765 -m hello

服务器把收到的每条消息加 "echo: " 前缀发回，用于验证客户端收发链路。
"""

import asyncio

import websockets


async def handler(ws):
    """每个客户端连接对应一个 handler 协程，互不影响。"""
    print(f"[服务端] 新连接：{ws.remote_address}")
    async for msg in ws:
        print(f"[服务端] 收到：{msg!r}")
        await ws.send(f"echo: {msg}")  # 回显，客户端应打印 [收到] echo: <msg>
    print("[服务端] 连接已关闭")


async def main():
    # serve() 返回服务对象；async with 退出时自动停服并等待已有连接收尾
    async with websockets.serve(handler, "127.0.0.1", 8765):
        print("[服务端] 监听 ws://127.0.0.1:8765 （Ctrl+C 退出）")
        await asyncio.Future()  # 永久挂起，直到 Ctrl+C 触发退出


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[服务端] 已停止")
