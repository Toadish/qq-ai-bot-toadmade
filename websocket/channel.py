# -*- coding: utf-8 -*-
"""
channel.py — 接入层通道抽象
============================
机器人核心（聊天逻辑）不关心消息从哪条路进来、从哪条路出去：
正向 WebSocket、反向 WebSocket、未来的 HTTP 上报……都实现同一个 Channel 接口。
这样「机器人核心」与「连接方式」解耦，新增通道不需要改动核心代码。

通道类型：
    WsClientChannel —— 正向连接：我们作为客户端去连框架（client.py 的角色）
    WsServerChannel —— 反向连接：框架（NapCat）作为客户端来连我们（本项目主用形态）
        NapCat 断线后会自动重连上来，服务端只需一直监听、随连随收。

事件钩子（子类覆写即可，基类默认什么都不做）：
    on_connect(...)     连接建立
    on_message(raw)     收到一条消息（str）
    on_disconnect(...)  连接断开
两个子类的钩子参数略有不同，见各自的 docstring。
"""

import asyncio
import contextlib
import json
import random
import uuid
from abc import ABC, abstractmethod

import websockets


class ChannelError(Exception):
    """通道层错误。例如：没有可用连接时调用了 send()。"""


def _make_echo() -> str:
    return "rpc-" + uuid.uuid4().hex[:8]


class Channel(ABC):
    """所有通道的公共接口：start / stop / send + 三个事件钩子。"""

    def __init__(self):
        self._pending: dict = {}   # echo → Future：API 调用（call）等待响应用
        # 收/处理解耦：接收循环只把报文放进队列，由独立派发任务串行调用
        # on_message。否则 on_message 里若发起 API 调用（等待响应），响应报文
        # 会被同一个被阻塞的接收循环挡住，永远读不到（死锁 → 超时）。
        self._msg_queue: asyncio.Queue = asyncio.Queue()
        self._dispatcher = None

    async def _dispatch(self) -> None:
        """独立派发任务：按到达顺序串行处理事件（顺序与旧实现一致）。"""
        while True:
            raw = await self._msg_queue.get()
            try:
                await self._safe(self.on_message, raw)
            finally:
                self._msg_queue.task_done()

    def _start_dispatch(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch())

    async def _stop_dispatch(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
            self._dispatcher = None

    # ---- 子类必须实现 ----

    @abstractmethod
    async def start(self) -> None:
        """启动通道（连接 / 开始监听）。约定：不阻塞，启动后立即返回。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止通道，释放资源。约定：可重复调用，不抛异常。"""

    @abstractmethod
    async def send(self, message: str) -> None:
        """发送一条文本消息。发送失败时抛 ChannelError（或 websockets 异常）。"""

    # ---- 事件钩子（子类按需覆写；基类默认空实现） ----

    async def on_connect(self, *args) -> None:
        pass

    async def on_message(self, raw: str) -> None:
        pass

    async def on_disconnect(self, *args) -> None:
        pass

    # ---- 内部工具 ----

    def _resolve_pending(self, raw: str) -> bool:
        """把一条收到的报文与待响应的 API 调用对上号。

        命中 echo → 完成对应 Future 并返回 True（调用方不应再把该报文当事件
        转发给 on_message）；否则返回 False。
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        if isinstance(data, dict) and data.get("echo") in self._pending:
            fut = self._pending.pop(data["echo"])
            if not fut.done():
                fut.set_result(data)
            return True
        return False

    async def call(self, action: str, params: dict, timeout: float = 10.0):
        """向框架发起一次 API 调用（带 echo 关联，等待响应）。

        用于需要读取返回值的能力（如 get_forward_msg 拉取合并转发内容）。
        超时抛 asyncio.TimeoutError；调用方按需捕获。
        """
        raise ChannelError("该通道不支持 API 调用")

    async def _safe(self, hook, *args) -> None:
        """调用事件钩子。钩子抛出的异常只打印、不打断通道主循环：
        单条消息处理出错不应杀死连接。"""
        try:
            await hook(*args)
        except Exception as exc:
            print(f"[Channel] 事件钩子异常（已忽略）：{exc!r}")


class WsClientChannel(Channel):
    """正向 WebSocket 通道：我们作为客户端去连框架的 ws 服务端。

    事件钩子参数：
        on_connect(uri)              连接建立，参数是服务地址
        on_message(raw)              收到一条文本消息
        on_disconnect(code, reason)  连接断开（正常或异常）
    """

    def __init__(self, uri: str):
        super().__init__()
        self.uri = uri
        self._ws = None    # websockets 连接对象；None 表示未连接
        self._task = None  # 后台接收循环任务

    @property
    def connected(self) -> bool:
        """当前是否有连接。"""
        return self._ws is not None

    async def start(self) -> None:
        """连接服务端，并启动后台接收循环（不阻塞，立即返回）。"""
        self._ws = await websockets.connect(self.uri)
        await self._safe(self.on_connect, self.uri)
        self._task = asyncio.create_task(self._recv_loop())
        self._start_dispatch()

    async def _recv_loop(self) -> None:
        """后台接收循环：一直接收，直到连接结束。"""
        code, reason = 1000, ""  # 循环正常结束 ⇒ 对端做了关闭握手（取不到具体码时按 1000 记录）
        try:
            async for raw in self._ws:
                if self._resolve_pending(raw):
                    continue    # API 调用响应：完成 Future，不再当事件转发
                await self._msg_queue.put(raw)   # 只入队，交给派发任务处理
        except websockets.ConnectionClosed as e:
            # 对端异常断开 / 网络断开时走这里（例如上次实测的 code=1006）
            code, reason = e.code, e.reason
        finally:
            await self._safe(self.on_disconnect, code, reason)

    async def send(self, message: str) -> None:
        """发送一条文本消息。未连接时抛 ChannelError。"""
        if self._ws is None:
            raise ChannelError(f"通道未连接（{self.uri}）")
        await self._ws.send(message)

    async def call(self, action: str, params: dict, timeout: float = 10.0):
        """向框架发起一次 API 调用并等待带 echo 的响应（RPC）。

        用于需要读取返回值的能力（如 get_forward_msg 拉取合并转发内容）。
        未连接时抛 ChannelError；超时抛 asyncio.TimeoutError。
        """
        if self._ws is None:
            raise ChannelError(f"通道未连接（{self.uri}），API 调用失败")
        echo = _make_echo()
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        message = json.dumps(
            {"action": action, "params": params, "echo": echo}, ensure_ascii=False
        )
        try:
            await self._ws.send(message)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)

    async def wait_until_closed(self) -> None:
        """挂起当前协程，直到连接结束（正常关闭或断开）。"""
        if self._task is not None:
            # shield：调用方被取消时（如 Ctrl+C）不连带取消接收循环，
            # 让调用方的 finally 有机会执行 stop() 做优雅关闭。
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        """主动停止：取消接收循环，优雅关闭连接。可重复调用。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass  # 已经关闭 / 已经断开等情况无需处理
            self._ws = None
        await self._stop_dispatch()


class WsServerChannel(Channel):
    """反向 WebSocket 通道：我们作为服务端，等框架（NapCat）连接上来。

    这是本项目的主用形态：
    - NapCat 配置里把「反向 WebSocket」指向本服务地址后，NapCat 断线会
      按它自己配置的重连间隔自动重连；
    - 服务端从不因单个连接断开而停服：断开的连接只是被移出连接表，
      新连接进来照常处理。因此「自动被重连」完全由 NapCat 侧负责，
      本侧不需要任何重连代码。

    事件钩子参数：
        on_connect(peer)       peer = (ip, port)
        on_message(raw)        收到一条文本消息
        on_disconnect(peer)    连接断开
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6701):
        super().__init__()
        self.host = host
        self.port = port
        self._server = None
        self._connections: set = set()  # 当前存活的连接（0 个 / 1 个 / 多个）

    @property
    def connected(self) -> bool:
        """当前是否有框架连接在线。"""
        return bool(self._connections)

    async def start(self) -> None:
        """开始监听（不阻塞，立即返回）。"""
        self._server = await websockets.serve(self._handle, self.host, self.port)
        self._start_dispatch()
        print(f"[WsServerChannel] 监听 ws://{self.host}:{self.port} （等待 NapCat 反向连接）")

    async def _handle(self, ws) -> None:
        """每个连接对应一个协程；断线重连会触发一个新的 _handle，互不影响。"""
        peer = ws.remote_address
        self._connections.add(ws)
        await self._safe(self.on_connect, peer)
        try:
            async for raw in ws:
                if self._resolve_pending(raw):
                    continue    # API 调用响应：完成 Future，不再当事件转发
                await self._msg_queue.put(raw)   # 只入队，交给派发任务处理
        except websockets.ConnectionClosed as e:
            # 框架断线属于正常现象（杀进程 / 重启 / 网络抖动），记录后继续等它重连
            print(f"[WsServerChannel] 连接断开 {peer}：code={e.code}（等待框架重连）")
        finally:
            self._connections.discard(ws)
            await self._safe(self.on_disconnect, peer)

    async def send(self, message: str) -> None:
        """向所有已连接的框架实例广播。没有连接时抛 ChannelError。

        当前只有一个 NapCat 时，广播等同于「发给它」；
        未来多实例时再改成按机器人的 QQ 号路由。
        """
        targets = list(self._connections)  # 快照，避免迭代期间连接表变化
        if not targets:
            raise ChannelError(f"当前没有框架连接（{self.host}:{self.port}），消息未发送")
        results = await asyncio.gather(
            *(ws.send(message) for ws in targets),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                # 注意：此时其他连接可能已经收到消息（部分成功）
                raise ChannelError(f"广播失败：{result!r}")

    async def call(self, action: str, params: dict, timeout: float = 10.0):
        """向框架发起一次 API 调用并等待带 echo 的响应（RPC）。

        用于需要读取返回值的能力（如 get_forward_msg 拉取合并转发内容）。
        没有框架连接时抛 ChannelError；超时抛 asyncio.TimeoutError。
        """
        targets = list(self._connections)
        if not targets:
            raise ChannelError(f"当前没有框架连接（{self.host}:{self.port}），API 调用失败")
        echo = _make_echo()
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        message = json.dumps(
            {"action": action, "params": params, "echo": echo}, ensure_ascii=False
        )
        try:
            results = await asyncio.gather(
                *(ws.send(message) for ws in targets), return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception):
                    raise ChannelError(f"API 调用发送失败：{result!r}")
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)

    async def stop(self) -> None:
        """停止监听，并断开所有现有连接。"""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        await self._stop_dispatch()


class SendLimiter:
    """发送限流器：所有主动发送统一排队（防腾讯风控）。

    规则（按序执行）：
      0. 前摇停顿 —— 每条消息发送前随机等待 pre_delay_min~max 秒，
         模拟真人"想一下再回"，避免秒回；第一条回复同样生效。
      1. 并发上限 1 —— 同一时刻只有一条消息在发送（单 worker 串行消费队列，
         天然满足）；max_concurrency 字段只作配置占位，当前实现恒为 1。
      2. 超长截断 —— 超过 max_message_chars 的文本回复截断并加省略号。
         以 "{" 开头的 JSON 报文（OneBot API 调用）不截断，否则会破坏 JSON 结构。
      3. 按字数加延迟 × 随机抖动 —— 每条消息发送完成后，
         sleep((base + len(文本)/chars_per_second) × uniform(jitter_min, jitter_max))。
         与篇幅成正比的时间间隔 + 随机抖动，让连续消息呈现真人节奏：
         完全规律的间隔本身就是机器特征。
      4. 失败退避 —— 发送失败时指数退避 base*2^(n-1)（封顶），成功后清零；
         连续失败达到 failure_alarm_threshold 次时打印告警横幅。
         失败不断连发只会加重风控，先冷静再说话。

    用法：
        limiter = SendLimiter(channel, **send_guard 配置)
        await limiter.send("回复内容")   # 入队即返回，消息稍后发出
        await limiter.stop()            # 停止前尽量把队里剩余消息发完
    """

    def __init__(
        self,
        channel,
        base_delay_seconds: float = 1.0,
        chars_per_second: float = 20.0,
        max_message_chars: int = 300,
        jitter_min: float = 0.8,
        jitter_max: float = 1.5,
        pre_delay_min_seconds: float = 0.0,
        pre_delay_max_seconds: float = 0.0,
        failure_backoff_base_seconds: float = 2.0,
        failure_backoff_max_seconds: float = 30.0,
        failure_alarm_threshold: int = 5,
        max_concurrency: int = 1,
    ):
        self._channel = channel
        self._base = base_delay_seconds
        self._cps = max(chars_per_second, 1.0)  # 防止除以 0
        self._max_chars = max_message_chars
        self._jitter_min = jitter_min
        self._jitter_max = jitter_max
        # 前摇：发送前随机停顿（min<=max 才生效；相等则固定停顿，便于测试）
        self._pre_delay_min = min(pre_delay_min_seconds, pre_delay_max_seconds)
        self._pre_delay_max = max(pre_delay_min_seconds, pre_delay_max_seconds)
        self._backoff_base = failure_backoff_base_seconds
        self._backoff_max = failure_backoff_max_seconds
        self._alarm_threshold = failure_alarm_threshold
        self._failures = 0          # 连续失败计数（成功后清零）
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = None

    async def _ensure_worker(self) -> None:
        """惰性启动唯一的工作协程。"""
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def _truncate(self, message: str) -> str:
        """超长截断。JSON 报文（OneBot API 调用）原样放行。"""
        if message.lstrip().startswith("{"):
            return message
        if len(message) <= self._max_chars:
            return message
        print(f"[SendLimiter] 消息超长（{len(message)}/{self._max_chars} 字符），已截断")
        return message[: self._max_chars - 1] + "…"

    async def _run(self) -> None:
        """唯一消费者：前摇 → 截断 → 发送 → 按字数抖动等待（或失败退避）→ 下一个。"""
        while True:
            message = await self._queue.get()
            try:
                # 前摇：发送前随机停顿，模拟真人想一下再回（第一条消息同样生效）
                if self._pre_delay_max > 0:
                    await asyncio.sleep(
                        random.uniform(self._pre_delay_min, self._pre_delay_max)
                    )
                message = self._truncate(message)
                try:
                    await self._channel.send(message)
                except Exception as exc:
                    self._failures += 1
                    # 指数退避：失败越多等越久（2s → 4s → 8s …，封顶 30s）
                    backoff = min(
                        self._backoff_base * (2 ** (self._failures - 1)),
                        self._backoff_max,
                    )
                    print(
                        f"[SendLimiter] 发送失败（连续 {self._failures} 次，"
                        f"退避 {backoff:.1f}s）：{exc!r}"
                    )
                    if self._failures >= self._alarm_threshold:
                        print("=" * 60)
                        print(
                            f"【发送告警】连续 {self._failures} 次发送失败："
                            f"可能已断连或触发风控，请人工检查。"
                        )
                        print("=" * 60)
                    await asyncio.sleep(backoff)
                else:
                    self._failures = 0  # 成功一次，失败计数清零
                    delay = self._base + len(message) / self._cps
                    # 随机抖动：让间隔不呈现机械规律
                    delay *= random.uniform(self._jitter_min, self._jitter_max)
                    await asyncio.sleep(delay)
            finally:
                self._queue.task_done()

    async def send(self, message: str) -> None:
        """把消息放进发送队列（不等待真正发出）。所有调用方共享同一条队列。"""
        await self._ensure_worker()
        await self._queue.put(message)

    async def stop(self, drain_timeout: float = 10.0) -> None:
        """停止：最多等 drain_timeout 秒让队内剩余消息发完，然后取消 worker。

        设计取舍：停机时「宁可丢发送、不可丢接收」——桥接关闭前收到的消息
        都已经处理完了，队里没发出去的回复丢掉即可，比为了发完而拖延关机
        更安全（拖延会让 NapCat 先断而桥还活着的窗口扩大）。
        """
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), drain_timeout)
        except asyncio.TimeoutError:
            pass  # 超时：剩余消息放弃
        self._worker.cancel()
        self._worker = None
