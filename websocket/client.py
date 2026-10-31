# -*- coding: utf-8 -*-
"""
最小完整 WebSocket 客户端（异步版，基于 channel 抽象）
======================================================
上一版的独立实现已收敛到 channel.WsClientChannel（正向通道），
本文件只负责命令行入口与输出格式，行为与上一版完全一致：
连接 → （可选）发送 → 接收循环打印 → 断开后优雅退出。

用法：
    python client.py                                  # 连默认地址，只收不发
    python client.py -m "hello"                       # 连接后先发一条 "hello"
    python client.py -u ws://127.0.0.1:8765 -m hello  # 连指定地址并发消息
"""

import argparse
import asyncio

from channel import WsClientChannel

# OneBot v11 框架（NapCat 等）默认正向 WebSocket 端口通常是 6700；
# 本地测试时用 echo_server.py 的 8765，或 server.py 的 6701。
DEFAULT_URI = "ws://127.0.0.1:6700"


class PrintChannel(WsClientChannel):
    """把通道事件翻译成命令行输出（保持上一版的输出风格）。"""

    async def on_connect(self, uri):
        print(f"[已连接] {uri}")

    async def on_message(self, raw):
        print(f"[收到] {raw}")

    async def on_disconnect(self, code, reason):
        print(f"[连接关闭] code={code} reason={reason}")


async def run(uri: str, message: str | None) -> None:
    channel = PrintChannel(uri)
    await channel.start()
    if message is not None:
        await channel.send(message)
        print(f"[已发送] {message}")
    try:
        await channel.wait_until_closed()  # 挂起直到连接结束
    finally:
        # 无论连接正常结束，还是 Ctrl+C 取消了本协程，都走优雅关闭
        await channel.stop()
    print("[结束] 已断开")


def main() -> None:
    parser = argparse.ArgumentParser(description="最小 WebSocket 客户端（正向通道演示）")
    parser.add_argument(
        "-u", "--uri", default=DEFAULT_URI,
        help=f"WebSocket 服务地址（默认 {DEFAULT_URI}）",
    )
    parser.add_argument(
        "-m", "--message", default=None,
        help="连接成功后立即发送的一条文本消息（可选）",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.uri, args.message))
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C，已优雅关闭")


if __name__ == "__main__":
    main()
