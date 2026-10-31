# -*- coding: utf-8 -*-
"""
fake_napcat.py — 测试用假 NapCat
=================================
模拟 NapCat 的三种行为，供 launcher.py 的守护/熔断逻辑做本地测试：

    python fake_napcat.py ok            # 连上桥接后常驻（模拟登录成功）
    python fake_napcat.py auto-exit 4   # 连上后 4 秒退出（模拟进程被误杀）
    python fake_napcat.py fail          # 立即退出（模拟登录失败）
"""

import asyncio
import sys

import websockets

URI = "ws://127.0.0.1:6701"


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

    if mode == "fail":
        print("[fake] 模拟登录失败，立即退出（code 1）")
        sys.exit(1)

    async with websockets.connect(URI) as ws:
        print("[fake] 已连上桥接（模拟登录成功）")
        if mode.startswith("auto-exit"):
            # 秒数作为独立参数传入（sys.argv[2]），例如：fake_napcat.py auto-exit 4
            seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
            await asyncio.sleep(seconds)
            print("[fake] 模拟进程被误杀，退出")
            return
        await asyncio.Future()  # 常驻，直到被外部杀死


if __name__ == "__main__":
    asyncio.run(main())
