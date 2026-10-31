# -*- coding: utf-8 -*-
"""
server.py — 反向 WebSocket 服务端入口（手动单跑模式）
====================================================
用法：
    python server.py             # 按 config.json 里的 host/port 监听（默认 127.0.0.1:6701）
    python server.py -p 9000     # 命令行临时改端口（优先级高于 config.json）

日常上机请用 launcher.py（桥接 + NapCat 守护 + 熔断告警一体）；
本文件只跑桥接，适合单跑调试桥接逻辑。
"""

import argparse
import asyncio

from channel import SendLimiter, WsServerChannel
from config import load_config


class DemoChannel(WsServerChannel):
    """演示通道：打印一切，并把 "ping" 回成 "pong"，方便本地验证收发链路。

    所有主动发送走 SendLimiter（并发 1 + 按字数延迟），
    与 launcher.py 的 BotChannel 同一套防风控规则。
    """

    def __init__(self, host: str, port: int, send_guard: dict):
        super().__init__(host, port)
        self.sender = SendLimiter(self, **send_guard)  # 配置字段名与参数一一对应

    async def on_connect(self, peer):
        print(f"[接入] 框架已连接：{peer}")

    async def on_disconnect(self, peer):
        print(f"[接入] 框架已断开：{peer}（重连交给 NapCat，无需干预）")

    async def on_message(self, raw: str):
        print(f"[消息] {raw}")
        if raw == "ping":
            await self.sender.send("pong")  # 演示：走限流队列回发
            print("[回复] pong 已入发送队列")

    async def stop(self):
        await self.sender.stop()  # 先尽量发完排队消息
        await super().stop()


async def main() -> None:
    # 优先级：命令行参数 > config.json > 内置默认值
    cfg = load_config()
    defaults = cfg["server"]

    parser = argparse.ArgumentParser(description="反向 WebSocket 服务端（手动单跑模式）")
    parser.add_argument(
        "--host", default=None,
        help=f"监听地址（默认取 config.json，当前为 {defaults['host']}）",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help=f"监听端口（默认取 config.json，当前为 {defaults['port']}）",
    )
    args = parser.parse_args()

    host = args.host if args.host is not None else defaults["host"]
    port = args.port if args.port is not None else defaults["port"]

    channel = DemoChannel(host, port, cfg["send_guard"])
    await channel.start()
    print(f"提示：另开终端运行 client.py -u ws://{host}:{port} -m ping 可模拟框架连接测试")
    try:
        await asyncio.Future()  # 常驻运行，直到 Ctrl+C
    finally:
        await channel.stop()
        print("[服务端] 已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[服务端] 收到 Ctrl+C，已优雅停止")
