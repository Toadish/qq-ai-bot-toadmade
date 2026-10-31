# -*- coding: utf-8 -*-
"""
launcher.py — 一键上机启动器（守护进程）
==========================================
职责与顺序：
  1. 先启动桥接：本进程内直接跑反向 WS 服务端（监听 config.json 的 host/port）；
  2. 大脑上机：长期记忆遗忘+计数、组装人设提示词、恢复短期记忆档；
  3. 确认桥接已监听后，再启动 NapCat（boot_command 带 QQ 号 = 快速登录）。
     ——「桥在，QQ 才在」：如果顺序反了，NapCat 先上线、桥还没起来，
        这段时间的 QQ 消息会永久丢失。
  4. 守护 NapCat：进程被误杀 / 崩溃退出 → 等待 restart_backoff_seconds 后自动拉起；
  5. 熔断：每次拉起后，若 login_timeout_seconds 内桥接没等到连接（= 登录失败），
     失败计数 +1；连续 max_login_failures 次 → 立刻停止并告警
     （控制台 + alarm.log + ALARM.txt 标记文件 + 蜂鸣），进程以退出码 1 结束；
     只要有一次登录成功，计数清零。
  6. 关闭顺序（Ctrl+C）：先停 NapCat → 大脑下机（长期记忆总结）→ 再停桥接，
     保证桥「从 NapCat 活着到 NapCat 死亡」全程在线，不漏消息。

用法：
    python launcher.py                     # 读 config.json 一键上机（自动请求管理员权限）
    python launcher.py --qr               # 首次扫码登录（二维码显示在 NapCat 控制台窗口）
    python launcher.py --config <文件>      # 指定配置（测试用）
    python launcher.py --no-elevate        # 不自动提权（测试/容器环境用）
"""

import argparse
import asyncio
import ctypes
import getpass
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 把工作区根加进模块搜索路径，才能 import 大脑与记忆（brain/、memory/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channel import SendLimiter, WsServerChannel  # noqa: E402
from config import load_config, resolve_path       # noqa: E402
from brain.brain import ChatBrain                 # noqa: E402
from brain.timesource import TimeSource           # noqa: E402
from memory.memory import MemoryStore             # noqa: E402

# 项目版本：工作区根的 VERSION 文件（ydj-bot v1.0.0）
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
PROJECT_VERSION = (
    _VERSION_FILE.read_text(encoding="utf-8").strip()
    if _VERSION_FILE.exists() else "0.0.0"
)


class BotChannel(WsServerChannel):
    """机器人通道：NapCat 反向连接的落点。

    - online 事件 = 「NapCat 已登录并连上桥接」，供守护循环判断登录成败；
    - 所有主动发送统一走 self.sender（SendLimiter），防风控；
    - 收到的每条报文转交给大脑（brain.handle_event）；未接大脑时仅打印。
    """

    def __init__(self, host: str, port: int, send_guard: dict):
        super().__init__(host, port)
        self.online = asyncio.Event()
        # 发送统一走限流器：配置里的 send_guard 字段名与 SendLimiter 参数一一对应
        self.sender = SendLimiter(self, **send_guard)
        self.brain = None  # 启动时由 main() 注入 ChatBrain

    async def on_connect(self, peer):
        self.online.set()
        print(f"[上线] NapCat 已连接：{peer}（登录成功）")

    async def on_disconnect(self, peer):
        self.online.clear()
        print(f"[离线] NapCat 断开：{peer}（若进程存活，它会在几秒内自动重连）")

    async def on_message(self, raw: str):
        if self.brain is not None:
            await self.brain.handle_event(raw)
        else:
            print(f"[消息] {raw[:200]}")  # 未接大脑时只打印，防刷屏

    async def stop(self):
        await self.sender.stop()  # 先尽量发完排队消息
        await super().stop()


class NapCatGuard:
    """NapCat 进程守护：拉起 → 等登录 → 看守；失败计数 → 熔断。"""

    def __init__(self, cfg: dict, channel: BotChannel, config_dir: Path,
                 qr_mode: bool = False):
        self.cfg = cfg["napcat"]
        self.channel = channel
        self.config_dir = config_dir  # shell_dir 相对配置文件所在目录解析
        self.qr_mode = qr_mode        # 首次扫码登录模式（不带 QQ 号启动）
        self.proc = None
        self.failures = 0
        self._qr_task = None          # 扫码图片打开任务
        self._started_epoch = 0.0
        # 告警文件路径（paths 组，相对配置文件目录）
        paths = cfg.get("paths", {})
        self._alarm_log = resolve_path(paths.get("alarm_log_file", "alarm.log"), config_dir)
        self._alarm_marker = resolve_path(paths.get("alarm_marker_file", "ALARM.txt"), config_dir)

    def _command(self):
        cwd = (self.config_dir / self.cfg["shell_dir"]).resolve()
        cmd = list(self.cfg["boot_command"])
        # 首次扫码模式：去掉末尾的 QQ 号参数（快速登录需要"登录过一次"，
        # 没登录态时带号启动会秒退；不带号则弹出登录窗口扫码）
        if self.qr_mode and len(cmd) > 1 and str(cmd[-1]).isdigit():
            cmd = cmd[:-1]
        # Windows 下 Popen 用【父进程】的工作目录解析可执行文件名，cwd= 只决定
        # 子进程的工作目录——裸文件名会找不到。这里把可执行文件解析成绝对路径。
        exe = Path(cmd[0])
        if not exe.is_absolute():
            candidate = cwd / exe
            if candidate.exists():
                cmd[0] = str(candidate.resolve())
        return cwd, cmd

    def start(self) -> None:
        """拉起 NapCat。经 PowerShell 包装（.bat）启动，与用户双击 napcat.bat 一致。

        排障结论（2026-10-28）：
        - 本机 .bat 默认用 PowerShell 打开，双击等效 PowerShell 包装；
        - 【根因】NapCat 注入 QQ 必须管理员权限，权限不足时 QQ 启动即崩
          （QQNT.dll 0x80000003）——此前 cmd/PowerShell/句柄实验均为烟雾弹；
        - 管理员模式下 NapCat 的扫码二维码直接以黑白块符号显示在它自己的
          控制台窗口（不是 QQ 登录窗），故这里不重定向输出，保留可见窗口。
        """
        cwd, cmd = self._command()
        if cmd[0].lower().endswith(".bat"):
            bat = cmd[0]
            args = " ".join(cmd[1:])
            ps_command = f"& '{bat}'" + (f" {args}" if args else "")
            process_cmd = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", ps_command,
            ]
        else:
            process_cmd = cmd
        # 独立控制台 + 输出不重定向：二维码/日志直接显示在 NapCat 自己的窗口
        self.proc = subprocess.Popen(
            process_cmd, cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self._started_epoch = time.time()
        if self.qr_mode:
            # 扫码模式：延迟打开 NapCat 生成的二维码图片（控制台黑白块对齐差、
            # 扫不出来；图片在 versions/*/resources/app/napcat/cache/qrcode.png）
            delay = float(self.cfg.get("qr_delay_seconds", 10))
            self._qr_task = asyncio.create_task(self._open_qr_image(delay, cwd))
        mode = "（扫码登录模式：稍后将自动打开二维码图片）" if self.qr_mode else "（快速登录）"
        print(f"[守护] 已拉起 NapCat{mode}（PID {self.proc.pid}）：{cmd}  @ {cwd}")

    async def _open_qr_image(self, delay: float, cwd: Path) -> None:
        """扫码模式下，延迟 delay 秒后打开 NapCat 的二维码图片。

        图片由 NapCat 启动几秒后才生成；找不到就每 5 秒重试，最多再等 30 秒。
        只打开本次启动后新生成的图片（按修改时间判断，避免打开旧的过期码）。
        """
        await asyncio.sleep(delay)
        launched_at = self._started_epoch
        for _ in range(6):
            candidates = list(
                Path(cwd).glob("versions/*/resources/app/napcat/cache/qrcode.png")
            )
            for p in candidates:
                try:
                    if p.exists() and p.stat().st_mtime >= launched_at - 5:
                        os.startfile(str(p))  # 用系统默认图片查看器打开
                        print(f"[守护] 已打开扫码图片：{p}")
                        return
                except OSError:
                    continue
            await asyncio.sleep(5)
        print("[守护] 未找到二维码图片（可手动打开 NapCat 的 cache\\qrcode.png 或看控制台窗口）")

    def kill(self) -> None:
        """结束 NapCat 进程树（taskkill /T 连子进程一起杀）。"""
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        self.proc = None
        if self._qr_task is not None and not self._qr_task.done():
            self._qr_task.cancel()

    async def wait_login(self, timeout: float) -> bool:
        """在 timeout 秒内等 NapCat 连上桥接。True = 登录成功。"""
        try:
            await asyncio.wait_for(self.channel.online.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def run_forever(self) -> int:
        """守护主循环。返回退出码：0 = 正常退出；1 = 熔断告警。"""
        while True:
            self.channel.online.clear()  # 清掉上次会话的在线标记，防止误判
            try:
                self.start()
                started = time.monotonic()
            except FileNotFoundError as exc:
                # 可执行文件不存在（路径/配置问题）：不崩溃，计入失败并提示
                self.failures += 1
                cwd, cmd = self._command()
                print(
                    f"[守护] 无法启动 NapCat：{cmd[0]} 不存在。"
                    f"请检查 config.json 的 napcat.shell_dir / boot_command（当前 cwd={cwd}）。"
                    f"连续失败 {self.failures}/{self.cfg['max_login_failures']}"
                )
                if self.failures >= self.cfg["max_login_failures"]:
                    alarm(self.cfg, self._alarm_log, self._alarm_marker)
                    return 1
                await asyncio.sleep(self.cfg["restart_backoff_seconds"])
                continue
            ok = await self.wait_login(self.cfg["login_timeout_seconds"])

            if ok:
                self.failures = 0
                print("[守护] 登录成功，进入看守状态（进程被杀会自动拉起）")
                code = await asyncio.to_thread(self.proc.wait)  # 阻塞直到进程退出
                self.proc = None
                elapsed = time.monotonic() - started
                if elapsed < float(self.cfg.get("fast_exit_hint_seconds", 10)):
                    print(
                        "[守护] ⚠ NapCat 启动后很快退出，常见原因：\n"
                        "  ① 从未扫码登录过 → 先跑 python launcher.py --qr 扫码登录一次；\n"
                        "  ② 正常版 QQ 正在运行（单实例冲突）→ 先退出正常版 QQ；\n"
                        "  ③ 登录态损坏 → 用 --qr 重新扫码。"
                    )
                print(
                    f"[守护] NapCat 进程退出（code={code}），"
                    f"{self.cfg['restart_backoff_seconds']} 秒后自动拉起"
                )
            else:
                self.failures += 1
                print(
                    f"[守护] 登录超时：{self.cfg['login_timeout_seconds']} 秒内桥接未收到连接，"
                    f"连续失败 {self.failures}/{self.cfg['max_login_failures']}"
                )
                self.kill()
                if self.failures >= self.cfg["max_login_failures"]:
                    alarm(self.cfg, self._alarm_log, self._alarm_marker)
                    return 1

            await asyncio.sleep(self.cfg["restart_backoff_seconds"])

    def stop(self) -> None:
        """停止 NapCat（守护循环退出时调用）。"""
        self.kill()


def alarm(cfg: dict, log_path: Path, marker_path: Path) -> None:
    """熔断告警：控制台横幅 + 告警日志追加 + 标记文件 + 蜂鸣。"""
    msg = (
        f"【熔断告警】NapCat 连续 {cfg['max_login_failures']} 次登录失败，"
        f"已停止自动拉起。请人工检查 QQ 登录状态（票据是否过期、是否需要重新扫码）"
        f"后重新运行 launcher.py。"
    )
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    banner = "\n" + "=" * 60 + "\n" + line + "\n" + "=" * 60
    print(banner)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 标记文件：外部脚本 / 未来的推送告警可以通过检测它来感知熔断
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(line + "\n", encoding="utf-8")
    print("\a", end="", flush=True)  # 终端蜂鸣


def ensure_admin() -> bool:
    """当前进程是否已有管理员权限。非 Windows / 异常时视为 True（不阻塞）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def try_elevate(argv: list) -> None:
    """非管理员时弹 UAC 以管理员重启自身；成功拉起则本进程直接退出。

    ShellExecuteW 返回值 >32 表示成功。失败（用户拒绝 UAC / 沙箱环境）
    时静默返回，由调用方决定继续跑还是提示。
    """
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, subprocess.list2cmdline(argv), None, 1
        )
    except Exception:
        return
    if ret > 32:
        sys.exit(0)  # 管理员实例已拉起，本进程功成身退


async def main() -> int:
    parser = argparse.ArgumentParser(description="一键上机启动器（桥接 + NapCat 守护）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认本目录 config.json）")
    parser.add_argument(
        "--qr", action="store_true",
        help="首次扫码登录模式：不带 QQ 号启动 NapCat，二维码显示在 NapCat 控制台窗口。"
             "登录过一次后，日常运行就不需要 --qr（快速登录）。",
    )
    parser.add_argument(
        "--no-elevate", action="store_true",
        help="不自动请求管理员权限（测试/容器环境用；日常请勿使用）",
    )
    parser.add_argument(
        "--version", action="store_true",
        help="显示版本号后退出",
    )
    args = parser.parse_args()

    if args.version:
        print(f"ydj-bot（会聊天的妖刀姬） v{PROJECT_VERSION}")
        return 0

    cfg = load_config(args.config)
    config_dir = Path(args.config if args.config else __file__).parent.resolve()
    print(f"[启动] ydj-bot v{PROJECT_VERSION} · {cfg['persona']['slug']} · 阶段一：会聊天")

    # API Key 引导：提权后新进程拿不到原 PowerShell 会话的环境变量，
    # 所以密钥存在配置文件同目录的密钥文件里（paths.apikey_file，不进版本库）。
    # 优先级：环境变量 > 密钥文件 > 交互输入（首次，自动保存）。
    key_env = cfg["llm"].get("api_key_env") or "DEEPSEEK_API_KEY"
    keyfile = resolve_path(cfg["paths"].get("apikey_file", ".apikey"), config_dir)
    env_key = os.environ.get(key_env, "").strip()
    file_key = keyfile.read_text(encoding="utf-8").strip() if keyfile.exists() else ""
    if not (env_key.startswith("sk-") or file_key.startswith("sk-")):
        if not args.no_elevate and sys.stdin.isatty():
            print("[启动] 未找到有效的 DeepSeek API Key（环境变量 / 密钥文件均无或格式异常）。")
            for _ in range(3):
                entered = getpass.getpass(
                    f"请输入 API Key（形如 sk- 开头的长串，回车跳过；保存到 {keyfile.name}）: "
                ).strip()
                if not entered:
                    break
                if entered.startswith("sk-") and len(entered) >= 20:
                    keyfile.parent.mkdir(parents=True, exist_ok=True)
                    keyfile.write_text(entered, encoding="utf-8")
                    print(f"[启动] 已保存到 {keyfile.name}（该文件请勿提交到版本库）。")
                    break
                print("[启动] 格式不像 DeepSeek Key（应为 sk- 开头、长度 20+），请重新粘贴。")
            else:
                print(f"[启动] 多次输入无效，已跳过（可稍后手动编辑 {keyfile}）。")

    # 权限检查：NapCat 注入 QQ 在本机必须管理员权限（否则 QQ 启动即被处决）
    if not ensure_admin() and not args.no_elevate:
        print("[启动] 检测到非管理员权限。NapCat 注入 QQ 需要管理员身份，"
              "正在请求 UAC 提权……")
        try_elevate(sys.argv)
        print("[启动] 提权未成功（已取消或环境不支持），将以非管理员权限继续，"
              "NapCat 可能启动异常。建议改用管理员终端重跑。")
    srv = cfg["server"]

    # ① 桥接先上线
    channel = BotChannel(srv["host"], srv["port"], cfg["send_guard"])
    try:
        await channel.start()
    except OSError as exc:
        print(f"[启动] 桥接监听失败（端口 {srv['port']} 可能被占用，"
              f"检查是否有别的 server.py / launcher.py 在跑）：{exc!r}")
        return 2
    print(f"[启动] 桥接已监听 ws://{srv['host']}:{srv['port']}")

    # ② 大脑上机：时间源（事件时间→联网北京时间，绝不用本地时钟）→
    #    长期记忆每日遗忘仪式 → 组装人设提示词 → 恢复短期记忆档
    brain = None
    try:
        timesource = TimeSource(cfg.get("timesource", {}))
        memory_file = resolve_path(
            cfg["paths"].get("memory_file", "../personas/{slug}/memory.json"),
            config_dir, slug=cfg["persona"]["slug"],
        )
        store = MemoryStore(memory_file, clock=timesource.last_known)
        brain = ChatBrain(channel, cfg, config_dir=config_dir, store=store,
                          timesource=timesource)
        await brain.startup()
        channel.brain = brain
    except Exception as exc:
        # 大脑初始化失败（如没配 API Key）：降级为"只收不回"模式，不阻塞上机
        print(f"[启动] 大脑初始化失败，降级为只打印模式：{exc!r}")
    print("[启动] 开始拉起 NapCat")

    # ③ 再拉起 NapCat 并守护
    guard = NapCatGuard(cfg, channel, config_dir, qr_mode=args.qr)
    try:
        return await guard.run_forever()
    finally:
        # ④ 关闭顺序：先停 NapCat → 大脑下机（总结记忆）→ 再停桥接
        print("[关闭] 正在停止 NapCat ...")
        guard.stop()
        if brain is not None:
            await brain.shutdown()   # 下机总结（长期记忆写入）
        print("[关闭] 正在停止桥接 ...")
        await channel.stop()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C（已按「先 NapCat 后桥接」的顺序收尾）")
        sys.exit(0)
