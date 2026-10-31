# -*- coding: utf-8 -*-
"""
brain.py — 聊天大脑（总指挥）
================================
接线：launcher.py 启动桥接后创建 ChatBrain，把每条 OneBot 报文交给
handle_event()；下机时调用 shutdown()。

职责闭环：
    上机 startup()  → 长期记忆遗忘+计数、组装 system prompt（人设+记忆）
    收消息          → 解析 → 搜罗图片 → 记短期记忆 → LLM 决策是否回复
                      → 回复（走发送限流器，可带表情）→ 记日志
    下机 shutdown() → 存短期记忆档 → 基于【完整日志】做长期记忆总结
                      → 写入回忆与人物档案

"话费不够"机制（usage.py 记账）：
    - 每日用量超过预算×比例，当天首次提醒一次——由 LLM 用"盘缠/话费"
      的比喻在回复里顺带转达；
    - 余额真不足（HTTP 402）时，用角色台词兜底回复一次。
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime
from pathlib import Path

import httpx

from memory.short_term import ShortTermMemory

from .emoji import EmojiLibrary
from .llm import BalanceError, DeepSeekClient, LLMError
from .mood import MoodStore
from .onebot import (build_image_segment, build_reply_segments,
                     build_send_action, build_send_qzone_action,
                     build_set_request_action, forward_nodes_to_text,
                     parse_event, parse_request_event, segments_to_text)
from .proactive import ProactiveAgent
from .timesource import TimeSource
from .usage import TokenUsage

# ---- 提示词常量 ----

DECISION_INSTRUCTION = (
    "\n\n请以角色身份判断要不要回复这条【新消息】。"
    "只回你在意的、想回的；不想回就 {\"reply\": false}。"
    "群聊里别人之间的闲谈，尽量不插话；只有话题与你有关、"
    "有人叫你、或你确有想说的话时才回。"
    "回复必须极短：一句话，尽量在 20 字以内（最多 40 字），"
    "短句，句尾常用省略号，符合你的人设。"
    "只输出 JSON：{\"reply\": true|false, \"text\": \"回复内容\""
    ", \"emoji\": \"可选关键词或省略\", \"mentioned\": [编号]或省略"
    ", \"mood_delta\": {维度:数值}或省略, \"global_mood_delta\": {维度:数值}或省略}"
    "（reply 为 false 时 text 留空、省略 emoji）"
    "若【新消息】准确提起了上面某条回忆（不只是泛泛而谈），"
    "把那条回忆的编号填进 mentioned 数组；没提起就省略该字段。"
    "若这则消息让你对说话者的情绪起了变化，加 \"mood_delta\": {\"喜\": 1, \"怒\": -2}"
    "（只写有变化的维度，每项 -5 到 5）；没变化就省略。"
    "若你的总体心境也因此起了变化，加 \"global_mood_delta\": {\"哀\": 1}"
    "（同样只写有变化的维度）；没变化就省略。"
    "回复语气与【你此刻的心境】和【你此刻对他的情绪】保持一致。"
)

# 群聊两步决策（沉默因子回路）：
# 第一次调用只判断「想不想回」+ 提起识别 + 情绪变化（不生成文本）；
# 想回时由代码掷沉默因子（角色级 group_silence_chance），放行才做第二次
# 调用生成回复——被沉默时省掉回复文本的 token。
GROUP_DECISION_INSTRUCTION = (
    "\n\n请以角色身份判断这条群聊【新消息】值不值得你开口。"
    "只回你在意的、想回的；群聊里别人之间的闲谈，尽量不插话；"
    "只有话题与你有关、有人叫你、或你确有想说的话时才想回。"
    "reply 只代表「想回」，回复内容稍后另生成，这里不要写文本。"
    "只输出 JSON：{\"reply\": true|false"
    ", \"mentioned\": [编号]或省略"
    ", \"mood_delta\": {维度:数值}或省略, \"global_mood_delta\": {维度:数值}或省略}"
    "若【新消息】准确提起了上面某条回忆（不只是泛泛而谈），"
    "把那条回忆的编号填进 mentioned 数组；没提起就省略该字段。"
    "若这则消息让你对说话者的情绪起了变化，加 \"mood_delta\": {\"喜\": 1, \"怒\": -2}"
    "（只写有变化的维度，每项 -5 到 5）；没变化就省略。"
    "若你的总体心境也因此起了变化，加 \"global_mood_delta\": {\"哀\": 1}"
    "（同样只写有变化的维度）；没变化就省略。"
)
GROUP_REPLY_INSTRUCTION = (
    "\n\n你要回复刚才那条群聊消息。写一句极短的话：尽量 20 字以内（最多 40 字），"
    "短句、句尾常用省略号，符合人设，不要 @ 任何人。"
    "只输出 JSON：{\"text\": \"回复内容\", \"emoji\": \"可选关键词或省略\"}"
)

# 情绪→表情的联想层：把表情库关键词注入决策提示词（每次上机构建一次），
# LLM 组织回复时若确有相应情绪，就在 JSON 里带上一个关键词。
# 表情库为空时整段省略，零额外成本。
EMOJI_HINT_TEMPLATE = (
    "\n【可选表情】{keywords}\n"
    "若你此刻确有相应情绪，可在 JSON 的 emoji 字段原样填其中一个关键词；"
    "没有合适的就省略该字段，不要硬凑。"
)
EMOJI_HINT_MAX_KEYWORDS = 12   # 提示词里最多带多少个关键词（控 token）

# 时间提示：每次决策都附带一行当前北京时间，是否用得上由 LLM 自行判断——
# 冷门时间词（晌午、打更、日上三竿……）也不怕漏判；话题与时间无关时无视即可，
# 绝不主动报时（保持真人感）。时间不可用时整行省略。
TIME_HINT_TEMPLATE = "【当前时间】{time}（{weekday}）\n"

REQUEST_DECISION_INSTRUCTION = (
    "\n\n这是一条【入群/加好友请求】。请以角色身份决定要不要同意。"
    "先看【近期对话】：若大人（QQ 见【大人】）近期提过要拉你进群、"
    "或介绍朋友来找你，应当同意；来者若是大人本人，直接同意。"
    "对陌生请求要谨慎：你向来避世修心、只与在意的人来往。"
    "只输出 JSON：{\"approve\": true|false, \"reason\": \"简短理由\"}"
    "（拒绝时的理由会转达给对方，须符合人设；同意时 reason 可留空）"
)

REQUEST_REFUSE_LINE = "妖刀姬近来避世修心，不相识者，恕不相见……"

BALANCE_LINE = (
    "大人……我这边的「盘缠」用尽了，暂时说不了话。"
    "改日大人续上，我们再谈。"
)

REMINDER = (
    "【系统提醒：请在本次回复里顺带用你自己的语气告诉大人——"
    "「盘缠」（调用额度）今日已用去八成，像提醒“话费不够”那样自然地说一句。"
    "只需顺带一提，不要长篇解释。】"
)

SUMMARY_TEMPLATE = (
    "以下是本次聊天（自上机到下机）的完整记录。请以 {name} 的视角，"
    "回顾这段相处，选出对你来说最重要、最值得记住的事情。\n"
    "要求：\n"
    "1. 最多 10 条，每条不超过 128 字节（约 42 个汉字），一句话写清一件事；\n"
    "2. 只记“以后再聊起会想起来的”事实与感受，不记寒暄与日常琐碎；\n"
    "3. 若某个具体的人对你特别重要或让你在意，在末尾单独输出一行：\n"
    "   人物更新 | QQ号 | 称呼 | 加标签:xx,xx | 减标签:xx | 好感度变化:+1/-2\n"
    "   （没有变化就省略这一行）\n"
    "4. 人物更新的 QQ 号必须从记录中各行的 (QQ:数字) 里原样抄录，"
    "禁止凭印象编造；记录里没出现过的 QQ 号，不要写人物更新。\n"
    "输出格式（纯文本）：\n"
    "- 回忆内容一\n"
    "- 回忆内容二\n"
    "人物更新 | ……"
)

_STRIP_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n|\n```\s*$")


class ChatBrain:
    def __init__(self, channel, cfg: dict, config_dir, store=None, llm=None,
                 timesource=None):
        self.channel = channel            # 桥接通道（有 sender 限流器）
        self.cfg = cfg
        self.config_dir = Path(config_dir)
        self.root = self.config_dir.parent  # 工作区根
        self.paths = cfg.get("paths", {})   # 运行路径（缺省时用内置默认）
        self.brain_cfg = cfg.get("brain", {})
        self.req_cfg = cfg.get("requests", {})  # 入群/加好友请求策略
        slug = cfg["persona"]["slug"]
        self.store = store                # 长期记忆 MemoryStore（可空）
        self.timesource = timesource or TimeSource(cfg.get("timesource", {}))
        self.llm = llm or DeepSeekClient(
            cfg["llm"],
            timeout=float(cfg["llm"].get("timeout_seconds", 60)),
            api_key=self._resolve_api_key(),
        )
        self.max_reply_chars = int(self.brain_cfg.get("max_reply_chars", 300))
        self._persona_meta = self._load_persona_meta()
        self.name = self._persona_meta.get("display_name") or "妖刀姬"
        # 群聊沉默因子：角色级（人设 meta.json 的 group_silence_chance 优先）。
        # LLM 想回群聊时先掷一把，随到沉默就不生成回复（省 token）；沉默的角色设高。
        meta_chance = self._persona_meta.get("group_silence_chance")
        self.group_silence_chance = float(
            meta_chance if meta_chance is not None
            else self.brain_cfg.get("group_silence_chance", 0.5)
        )
        # 群聊回复黑名单：这些 QQ 的群消息直接跳过（零 token，如群友的 bot）
        self.group_reply_blacklist = {
            str(qq) for qq in (self.brain_cfg.get("group_reply_blacklist") or [])
        }
        self.stm = ShortTermMemory(compress_fn=self._compress, **cfg["short_term"])
        self.emoji = EmojiLibrary(
            self._path("images_dir", "../images"),
            collect=bool(self.brain_cfg.get("collect_received_images", True)),
            max_files=int(self.brain_cfg.get("collect_max_files", 300)),
            max_file_bytes=int(self.brain_cfg.get("collect_max_file_bytes", 10 * 1024 * 1024)),
            min_interval_messages=int(self.brain_cfg.get("emoji_min_interval_messages", 4)),
            cooldown_seconds=float(self.brain_cfg.get("emoji_cooldown_seconds", 30)),
            collected_chance=float(self.brain_cfg.get("emoji_collected_chance", 0.05)),
        )
        self.usage = TokenUsage(
            self._path("usage_file", "../brain/usage.json"),
            daily_budget_tokens=int(cfg["llm"].get("daily_budget_tokens", 200000)),
            warn_ratio=float(cfg["llm"].get("warn_ratio", 0.8)),
        )
        mood_cfg = dict(cfg.get("mood", {}))
        # 平复因子是角色级参数：人设 meta.json 的 mood_calm_factor 优先，
        # 否则用 config 的 mood.calm_factor（换人设自动换平复速度）
        calm = float(self._persona_meta.get("mood_calm_factor")
                     or mood_cfg.get("calm_factor", 1.0))
        mood_cfg["calm_factor"] = calm
        self.mood = MoodStore(
            self._path("mood_file", "../brain/mood.json"),
            clock=self.timesource.last_known,
            favor_of=self._favor_of,
            **mood_cfg,
        )
        self.proactive = ProactiveAgent(self)   # 主动发起话题/说说（事件驱动）
        self.log_dir = self._path("sessions_dir", "../personas/{slug}/sessions", slug=slug)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self._path("stm_state_file", "../brain/stm_state.json")
        self.system_prompt = ""
        self._emoji_hint = ""      # 表情关键词注入段（上机时构建，空库则留空）
        self._mention_map = {}     # 回忆编号→id（提起加分用；上机时随注入块构建）
        self.self_id = None
        self._lock = asyncio.Lock()   # 消息处理串行化（防并发乱了记忆与节奏）
        self._fallback_sent = False   # 余额兜底台词每会话最多一次

    def _path(self, key: str, default: str, slug: str | None = None) -> Path:
        """按 config 的 paths 组解析运行路径（相对路径基于配置文件目录）。"""
        value = self.paths.get(key, default)
        if slug:
            value = value.replace("{slug}", slug)
        p = Path(value)
        if not p.is_absolute():
            p = self.config_dir / p
        return p.resolve()

    # ---------------- 上机 / 下机 ----------------

    def _resolve_api_key(self) -> str:
        """API Key 解析：环境变量 → apikey 文件（paths.apikey_file）→ 空串。

        提权（UAC）后新进程拿不到原 PowerShell 会话的环境变量，
        所以密钥允许存放在配置文件同目录的 .apikey 文件（不进版本库），
        由 launcher 首次运行时交互引导写入。
        只接受形如 sk- 开头的有效密钥，避免把损坏内容当密钥发给 API。
        """
        env_name = self.cfg["llm"].get("api_key_env") or "DEEPSEEK_API_KEY"
        key = os.environ.get(env_name, "").strip()
        if key:
            return key
        keyfile = self._path("apikey_file", ".apikey")
        if keyfile.exists():
            key = keyfile.read_text(encoding="utf-8").strip()
            if key:
                return key
            print("[大脑] 密钥文件为空，已忽略。")
        return ""

    async def startup(self) -> None:
        """上机：刷新时间源（事件时间→联网北京时间）→ 长期记忆每日遗忘仪式
        → 组装 system prompt → 恢复短期记忆档。"""
        await self.timesource.refresh()
        if self.store is not None:
            self.store.session_start()
        # 情绪上机：先按离线模式结算（纯自然衰减）→ 切在线 →
        # 有记录的人继承其长期情绪、没记录的人继承总体心境
        self.mood.set_online(True)
        if self.store is not None:
            self.mood.init_from_people(self.store.data.get("people", {}))
        self.proactive.start()   # 主动话题/说说后台循环
        self.system_prompt = self._build_system_prompt()
        self._emoji_hint = self._build_emoji_hint()
        if self.state_path.exists():
            try:
                self.stm = ShortTermMemory.load(
                    self.state_path, compress_fn=self._compress, **self.cfg["short_term"]
                )
                print("[大脑] 已从存档恢复短期记忆（崩溃恢复）")
            except Exception as exc:
                print(f"[大脑] 短期记忆存档损坏，忽略：{exc!r}")
        print(f"[大脑] 上机完成：{self.name}。{self.usage.summary()}")

    async def shutdown(self) -> None:
        """下机：刷新时间源 → 存档短期记忆 → 基于完整日志做长期记忆总结
        （每天每一次下机都会检查当日记忆；跨天时记忆引擎会补做当日遗忘）
        → 关闭 LLM 与时间源。"""
        print("[大脑] 下机流程开始")
        try:
            await self.proactive.stop()   # 先停主动行为循环
        except Exception as exc:
            print(f"[大脑] 主动循环停止异常：{exc!r}")
        try:
            await self.timesource.refresh()
        except Exception as exc:
            print(f"[大脑] 时间源刷新失败（按无时间继续）：{exc!r}")
        try:
            self.stm.save(self.state_path)
        except OSError as exc:
            print(f"[大脑] 短期记忆存档失败：{exc!r}")
        messages = self._summary_messages()
        if messages is not None:
            try:
                content, usage = await self.llm.chat(messages)
                self.usage.add(usage)
                self._apply_summary(_STRIP_FENCE.sub("", content.strip()))
            except Exception as exc:
                print(f"[大脑] 下机总结失败（长期记忆本次不写入）：{exc!r}")
        # 情绪收尾：切离线（此后总心境只自然回基线，不受任何人牵引）→
        # 各人的分情绪按 long_term_merge 牵引长期情绪、长期情绪按
        # long_term_decay 向基线回落（双向牵引，不会只涨不跌）
        try:
            self.mood.set_online(False)
            if self.store is not None:
                mood_cfg = self.cfg.get("mood", {})
                alpha = float(mood_cfg.get("long_term_merge", 0.2))
                beta = float(mood_cfg.get("long_term_decay", 0.05))
                base = dict(self.mood.baseline)
                people = self.store.data.get("people", {})
                for qq, snap in self.mood.per_person_snapshot().items():
                    if qq in people:
                        self.store.update_person_mood(
                            qq, snap["mood"], alpha=alpha, beta=beta, baseline=base
                        )
        except Exception as exc:
            print(f"[大脑] 情绪收尾失败：{exc!r}")
        await self.llm.close()
        await self.timesource.close()
        print(f"[大脑] 下机完成。{self.usage.summary()}")

    # ---------------- 消息入口 ----------------

    async def handle_event(self, raw: str) -> None:
        """处理一条桥接事件（串行化）。"""
        async with self._lock:
            await self._handle(raw)

    async def _handle(self, raw: str) -> None:
        event = parse_event(raw)
        if event is None:
            return
        # 时间源：每条事件都带 NapCat 的 `time`（Unix 秒）→ 喂给时间源。
        # 这是最可信的时间来源，忘了喂的话日更逻辑只能靠联网兜底。
        self.timesource.observe_event_time(event.get("time"))
        post = event.get("post_type")
        if post == "meta_event" and event.get("meta_event_type") == "lifecycle":
            self.self_id = event.get("self_id")
            print(f"[大脑] 框架上报 self_id={self.self_id}")
            return
        if post == "request":
            self.proactive.touch()   # 请求也是真实活动
            await self._handle_request(event)
            return
        if post != "message":
            return    # 心跳/通知等框架噪音：不算活动、不打扰她的清静
        if self.self_id is not None and event.get("user_id") == self.self_id:
            return  # 自己的消息（保险；NapCat 已设 reportSelfMessage=false）
        self.proactive.touch()   # 只有真正的聊天消息才算"活动"
        msg_type = event.get("message_type", "private")
        channel_label = f"群{event.get('group_id')}" if msg_type == "group" else "私聊"
        if msg_type == "group":
            self.proactive.note_group(event.get("group_id"))   # 记住所在群（主动话题用）
        segments = event.get("message") or []
        text = segments_to_text(segments, self.self_id)
        # 合并转发消息：读出内容拼进正文（inline content 优先，否则 get_forward_msg）
        for seg in segments:
            if seg.get("type") != "forward":
                continue
            fwd = await self._read_forward(seg)
            if fwd:
                text += "\n【合并转发】\n" + fwd
                break   # 一条消息含多个转发块时只取第一个（控制篇幅）
        sender = (event.get("sender") or {}).get("nickname") or str(event.get("user_id", "?"))
        sender_qq = str(event.get("user_id", ""))
        # 群聊回复黑名单：这些 QQ（如群友的 bot）的消息直接跳过——
        # 不决策、不记短期记忆、不写日志（零 token，杜绝 bot 互聊死循环）
        if msg_type == "group" and sender_qq in self.group_reply_blacklist:
            print(f"[大脑] 群聊黑名单（QQ {sender_qq}）：跳过")
            return

        # 搜罗收到的图片（本地路径 / file:// URI / http(s) URL 三种来源都收）。
        # 受 brain.collect_received_images 控制：现已切断（等做 OCR 识图后再接），
        # 关闭时连地址打印都不做，避免日志噪音。
        if self.brain_cfg.get("collect_received_images", True):
            for seg in segments:
                if seg.get("type") == "image":
                    f = (seg.get("data") or {}).get("file") or ""
                    if f:
                        print(f"[大脑] 收到图片，地址：{f[:120]}")  # 排障可观测
                    saved = await self._collect_incoming_image(f)
                    if saved:
                        print(f"[大脑] 搜罗了一张图片：{saved.name}")

        await self.stm.add_turn("user", sender, text, channel=channel_label)
        # 日志带上发送者 QQ：下机总结时 LLM 才能"抄"到准确 QQ 号（防编造）
        self._log(f"[{channel_label}] {sender}(QQ:{event.get('user_id', '?')})：{text}")

        if msg_type == "group":
            decision = await self._decide_group(text, sender_qq)
        else:
            decision = await self._decide(text, sender_qq)
        if decision is None:
            return
        reply, emoji_kw, mention_ids, mood_delta, global_delta = decision
        # 提起加分：有人准确提起回忆 → 计数 +1（每天每条限一次，跨重启持久化）。
        # 无论她回不回复，提起本身都算数。
        if self.store is not None and mention_ids:
            credited = self.store.mark_mentioned(mention_ids)
            if credited:
                print(f"[大脑] 提起加分：{credited} 条回忆被准确提起")
        # 总情绪（心境）：LLM 报的变化落账
        if global_delta:
            self.mood.apply_global_delta(global_delta)
        # 按人分化的情绪：LLM 报的变化落账（先衰减后加减，限幅+边界）
        if sender_qq and mood_delta:
            self.mood.apply_delta(sender_qq, sender, mood_delta)
        if not reply:
            return
        segs = build_reply_segments(reply)
        # 表情：优先用 LLM 情绪联想给的关键词；没给/不认识则回退文本关键词匹配
        img = self.emoji.pick_keyword(emoji_kw) if emoji_kw else None
        if img is None:
            img = self.emoji.pick(reply)
        if img:
            segs.append(build_image_segment(img))
        action = json.dumps(build_send_action(event, segs), ensure_ascii=False)
        await self.channel.sender.send(action)   # 统一走发送限流器（防风控）
        await self.stm.add_turn("assistant", self.name, reply, channel=channel_label)
        self._log(f"[{channel_label}] {self.name}：{reply}" + (f" [表情]{img.name}" if img else ""))
        self.proactive.touch()   # 自己发了消息 = 有活动

    # ---------------- 请求事件（好友申请 / 入群申请 / 入群邀请） ----------------

    async def _collect_incoming_image(self, f: str):
        """把收到的图片入库。三种来源：
        本地路径 → 直接收藏；file:// URI → 归一化后收藏；
        http(s) URL → 下载字节后收藏（限量限大小，失败静默跳过）。
        """
        if not f:
            return None
        if f.startswith("file://"):
            f = f[len("file://"):]
        m = re.match(r"^/([A-Za-z]:/.*)$", f)   # /C:/... → C:/...
        if m:
            f = m.group(1)
        if f.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                    resp = await c.get(f)
                    if resp.status_code != 200:
                        return None
                    blob = resp.content
            except Exception as exc:
                print(f"[大脑] 图片下载失败（跳过）：{exc!r}")
                return None
            suffix = Path(f.split("?")[0]).suffix or ".jpg"
            return self.emoji.collect_bytes(blob, suffix)
        return self.emoji.collect_image(f)

    async def _read_forward(self, seg: dict) -> str:
        """读取一条合并转发消息的内容文本。

        inline content（NapCat 数组格式常带）优先；否则用 get_forward_msg
        通过通道 RPC 拉取。失败返回空串（就当没读到，不阻塞主流程）。
        """
        data = seg.get("data") or {}
        nodes = data.get("content")
        if isinstance(nodes, list) and nodes:
            return forward_nodes_to_text(nodes)
        mid = data.get("id") or data.get("message_id")
        if not mid or not hasattr(self.channel, "call"):
            return ""
        try:
            resp = await self.channel.call(
                "get_forward_msg", {"message_id": str(mid)}, timeout=10.0
            )
        except Exception as exc:
            print(f"[大脑] 读取合并转发失败：{exc!r}")
            return ""
        messages = ((resp or {}).get("data") or {}).get("messages")
        if not isinstance(messages, list):
            return ""
        return forward_nodes_to_text(messages)

    @staticmethod
    def _request_label(info: dict) -> str:
        """请求 → 给人看的一句话描述（日志 / LLM 提示词共用）。"""
        user = info.get("user_id")
        if info["kind"] == "friend":
            return f"好友申请（QQ {user}）"
        gid = info.get("group_id")
        if info["kind"] == "group_add":
            return f"入群申请（QQ {user} 申请加入群 {gid}）"
        return f"入群邀请（QQ {user} 邀请进群 {gid}）"

    async def _handle_request(self, event: dict) -> None:
        """处理一条请求事件。策略见 config 的 requests 组（mode / 类型开关）。

        mode=llm 时决策失败（网络/余额）→ 只记日志、不发送任何动作：
        请求留在框架里保持待处理，由大人人工处置，绝不擅自放行陌生人。
        """
        info = parse_request_event(event)
        if info is None:
            return
        if not self.req_cfg.get(info["kind"], True):
            return  # 该类型被配置关掉：静默忽略
        label = self._request_label(info)
        comment = f"，附言：{info['comment']}" if info["comment"] else ""
        mode = self.req_cfg.get("mode", "off")

        if mode == "off":
            self._log(f"[请求] {label}{comment}（mode=off，仅记录）")
            return
        if mode == "approve_all":
            approve, reason = True, ""
        elif mode == "refuse_all":
            approve, reason = False, REQUEST_REFUSE_LINE
        else:  # llm：由角色按人设决策（计入"话费"）
            decision = await self._decide_request(info)
            if decision is None:
                self._log(f"[请求] {label}{comment}（决策失败，暂不处理，请人工处置）")
                return
            approve, reason = decision

        action = json.dumps(
            build_set_request_action(info, approve, reason), ensure_ascii=False
        )
        await self.channel.sender.send(action)   # 统一走发送限流器
        # 同意入群申请/邀请 → 建立这个群的发送通道（主动事件选取用，长期记忆）
        if approve and self.store is not None and info["kind"] in ("group_add", "group_invite"):
            self.store.note_group_channel(event.get("group_id"))
        verdict = "同意" if approve else "拒绝"
        note = f"（理由：{reason}）" if (not approve and reason) else ""
        self._log(f"[请求] {label}{comment} → {verdict}{note}")
        # 记进短期记忆：让主大脑"知道"收到过这条请求、自己怎么处置的，
        # 事后被问起才不会答"没收到申请"。
        await self.stm.add_turn(
            "system", "系统",
            f"收到{label}，已{verdict}" + (f"，{reason}" if (not approve and reason) else ""),
            channel="请求",
        )

    async def _decide_request(self, info: dict):
        """LLM 决策是否放行请求。返回 (approve: bool, reason: str)；失败返回 None。

        决策必须带上下文：短期记忆（大人是否提前打过招呼）+ 当前时间 +
        system prompt 里的【大人】QQ 与人物层——否则"提前说好要拉她进群"这类
        约定她根本想不起来，只会把大人当陌生人硬生生拒绝。
        """
        ctx = self.stm.context_for_prompt()
        base = (
            f"【请求】{self._request_label(info)}\n"
            f"【附言】{info['comment'] or '（无）'}"
        )
        if ctx:
            base = f"{ctx}\n\n{base}"
        user_content = self._time_hint() + base + REQUEST_DECISION_INSTRUCTION
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            content, usage = await self.llm.chat(
                messages, response_format={"type": "json_object"}
            )
        except BalanceError as exc:
            print(f"[大脑] 请求决策余额不足（暂不处理）：{exc}")
            return None
        except LLMError as exc:
            print(f"[大脑] 请求决策失败（暂不处理）：{exc}")
            return None
        self.usage.add(usage)
        content = _STRIP_FENCE.sub("", content.strip())
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict) or "approve" not in data:
            # 输出不合格式：视为决策失败（不擅自拒绝，保持待处理）
            print("[大脑] 请求决策输出格式异常（暂不处理）")
            return None
        approve = bool(data.get("approve", False))
        reason = str(data.get("reason") or "").strip()[:100]
        return approve, reason

    # ---------------- 说说（能力层，暂未接线） ----------------

    async def publish_qzone(self, content: str, images=None, ugc_right: int = 1) -> bool:
        """发表一条 QQ 空间说说。返回是否真的发出。

        目前只是能力层：没有任何触发（不是大人下指令才发）。预留给
        「主动发起话题」功能——由 LLM 决定想发时，草拟内容后调用本方法。
        走统一发送限流器；发布内容记入会话日志。
        """
        content = (content or "").strip()
        if not content:
            return False
        content = content[:500]   # 安全上限（说说正文足够长，这里防手滑）
        action = json.dumps(
            build_send_qzone_action(content, images=images, ugc_right=ugc_right),
            ensure_ascii=False,
        )
        await self.channel.sender.send(action)
        self._log(f"[说说] {content}")
        print(f"[大脑] 已发表说说：{content[:60]}")
        return True

    # ---------------- LLM 调用 ----------------

    async def _decide(self, text: str, sender_qq: str = ""):
        """一次调用完成「是否回复 + 回复内容 + 情绪表情 + 提起识别 + 情绪变化」
        （JSON 模式）。返回 (回复文本|None, emoji关键词, 提起id列表, mood_delta)，
        硬失败返回 None。
        """
        time_hint = self._time_hint()
        mood_line = "【你此刻的心境】" + self.mood.global_line() + "\n"
        if sender_qq:
            mood_line += "【你此刻对他的情绪】" + self.mood.line_for(sender_qq) + "\n"
        ctx = self.stm.context_for_prompt()
        base = f"{ctx}\n\n【新消息】{text}" if ctx else f"【新消息】{text}"
        user_content = time_hint + mood_line + base
        system = self.system_prompt
        if self.usage.warning_pending():
            system += "\n\n" + REMINDER  # “话费不够”提醒（当日一次）
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content + DECISION_INSTRUCTION + self._emoji_hint},
        ]
        try:
            content, usage = await self.llm.chat(
                messages, response_format={"type": "json_object"}
            )
        except BalanceError as exc:
            print(f"[大脑] 余额不足：{exc}")
            if not self._fallback_sent:
                self._fallback_sent = True
                return BALANCE_LINE, "", [], {}, {}
            return None
        except LLMError as exc:
            print(f"[大脑] 调用失败（本次不回复）：{exc}")
            return None
        self.usage.add(usage)
        content = _STRIP_FENCE.sub("", content.strip())
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            data = {"reply": True, "text": content}  # 没按 JSON 出：容忍为纯文本
        mention_ids = self._resolve_mentioned(data.get("mentioned"))
        mood_delta = data.get("mood_delta")
        if not isinstance(mood_delta, dict):
            mood_delta = {}
        global_delta = data.get("global_mood_delta")
        if not isinstance(global_delta, dict):
            global_delta = {}
        if not data.get("reply"):
            return None, "", mention_ids, mood_delta, global_delta
        text_reply = (data.get("text") or "").strip()
        if not text_reply:
            return None, "", mention_ids, mood_delta, global_delta
        emoji_kw = (data.get("emoji") or "").strip()
        return (text_reply[: self.max_reply_chars], emoji_kw, mention_ids,
                mood_delta, global_delta)

    async def _decide_group(self, text: str, sender_qq: str = ""):
        """群聊两步决策（沉默因子回路）：先判「想不想回」→ 掷沉默因子 →
        放行才生成回复。返回与 _decide 相同的元组。

        情绪变化与提起识别在第一次调用里报：被沉默时照常落账——
        看完消息心情变了、只是没开口（像真人）。
        """
        time_hint = self._time_hint()
        mood_line = "【你此刻的心境】" + self.mood.global_line() + "\n"
        if sender_qq:
            mood_line += "【你此刻对他的情绪】" + self.mood.line_for(sender_qq) + "\n"
        ctx = self.stm.context_for_prompt()
        base = f"{ctx}\n\n【新消息】{text}" if ctx else f"【新消息】{text}"
        system = self.system_prompt
        if self.usage.warning_pending():
            system += "\n\n" + REMINDER  # “话费不够”提醒（当日一次）
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": time_hint + mood_line + base + GROUP_DECISION_INSTRUCTION},
        ]
        try:
            content, usage = await self.llm.chat(
                messages, response_format={"type": "json_object"}
            )
        except BalanceError as exc:
            print(f"[大脑] 余额不足：{exc}")
            if not self._fallback_sent:
                self._fallback_sent = True
                return BALANCE_LINE, "", [], {}, {}
            return None
        except LLMError as exc:
            print(f"[大脑] 调用失败（本次不回复）：{exc}")
            return None
        self.usage.add(usage)
        content = _STRIP_FENCE.sub("", content.strip())
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            data = {}
        mention_ids = self._resolve_mentioned(data.get("mentioned"))
        mood_delta = data.get("mood_delta")
        if not isinstance(mood_delta, dict):
            mood_delta = {}
        global_delta = data.get("global_mood_delta")
        if not isinstance(global_delta, dict):
            global_delta = {}
        if not data.get("reply"):
            return None, "", mention_ids, mood_delta, global_delta
        # 沉默因子：想回也先掷一把。随到沉默 → 不生成回复（省 token）
        if random.random() < self.group_silence_chance:
            print("[大脑] 群聊想回，但沉默因子生效（没开口）")
            return None, "", mention_ids, mood_delta, global_delta
        # 放行：第二次调用生成回复（上下文轻：时间 + 心境 + 消息本身）
        reply_base = "\n".join(
            ln for ln in (
                time_hint.strip(),
                "【你此刻的心境】" + self.mood.global_line(),
                ("【你此刻对他的情绪】" + self.mood.line_for(sender_qq)) if sender_qq else "",
                f"【新消息】{text}",
            ) if ln
        )
        try:
            content2, usage2 = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": reply_base + GROUP_REPLY_INSTRUCTION},
                ],
                response_format={"type": "json_object"},
            )
        except (BalanceError, LLMError) as exc:
            print(f"[大脑] 回复生成失败（本次不回复）：{exc}")
            return None, "", mention_ids, mood_delta, global_delta
        self.usage.add(usage2)
        content2 = _STRIP_FENCE.sub("", content2.strip())
        try:
            data2 = json.loads(content2 or "{}")
        except json.JSONDecodeError:
            data2 = {"text": content2}   # 没按 JSON 出：容忍为纯文本
        text_reply = (data2.get("text") or "").strip()
        if not text_reply:
            return None, "", mention_ids, mood_delta, global_delta
        emoji_kw = (data2.get("emoji") or "").strip()
        return (text_reply[: self.max_reply_chars], emoji_kw, mention_ids,
                mood_delta, global_delta)

    def _resolve_mentioned(self, mentioned) -> list:
        """把 LLM 报的回忆编号解析成真实 id；编造/不认识的编号直接丢弃。"""
        ids = []
        if isinstance(mentioned, list):
            for x in mentioned:
                mid = self._mention_map.get(str(x).strip())
                if mid and mid not in ids:
                    ids.append(mid)
        return ids

    def _time_hint(self) -> str:
        """每次决策都附带一行当前时间（约 20 token），用不用由 LLM 自行判断。

        不靠关键词触发——冷门时间词（晌午、打更、日上三竿……）也不会漏判；
        话题与时间无关时角色应当无视（不主动报时，保持真人感）。
        时间不可用时省略整行。
        """
        now = self.timesource.last_known()
        if now is None:
            return ""
        weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[now.weekday()]
        return TIME_HINT_TEMPLATE.format(
            time=f"{now:%Y-%m-%d %H:%M}", weekday=weekday
        )

    async def _compress(self, turns, instruction: str) -> str:
        """短期记忆压缩回调（供 ShortTermMemory 调用）。"""
        content, usage = await self.llm.chat(
            [{"role": "user", "content": instruction}],
            max_tokens=int(self.brain_cfg.get("compress_max_tokens", 400)),
        )
        self.usage.add(usage)
        return content

    # ---------------- 下机总结 ----------------

    def _summary_messages(self):
        """基于【今日完整日志】构造总结请求；日志过短时返回 None（跳过）。"""
        path = self.log_dir / f"{datetime.now():%Y%m%d}.log"
        min_bytes = int(self.brain_cfg.get("summary_min_log_bytes", 50))
        max_chars = int(self.brain_cfg.get("summary_max_chars", 16000))
        if not path.exists() or path.stat().st_size < min_bytes:
            return None
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = "……（更早的记录略）\n" + text[-max_chars:]
        prompt = SUMMARY_TEMPLATE.format(name=self.name)
        return [{"role": "user", "content": prompt + "\n\n【本次聊天完整记录】\n" + text}]

    def _apply_summary(self, content: str) -> None:
        memories = []
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("- "):
                memories.append(line[2:].strip())
            elif line.startswith("人物更新"):
                try:
                    self._apply_person_update(line)
                except Exception as exc:
                    print(f"[大脑] 人物更新解析失败：{exc!r}")
        if self.store is not None:
            n = self.store.session_end(memories)
            print(f"[大脑] 长期记忆已写入 {n} 条")

    def _apply_person_update(self, line: str) -> None:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or self.store is None:
            return
        qq = parts[1]
        # 防幻觉：只接受今日会话日志里真实出现过的 QQ 号（日志行的 (QQ:数字)）。
        # 早期版本让 LLM 凭印象填 QQ，出现过"群号/乱猜的号"变成幻影人物。
        known = self._log_known_qqs()
        if known and qq not in known:
            print(f"[大脑] 人物更新 QQ {qq} 未在今日日志出现（疑似幻觉），已忽略")
            return
        name = parts[2] if len(parts) > 2 and parts[2] else None
        labels_add, labels_del, favor_delta = [], [], 0
        for p in parts[3:]:
            if p.startswith("加标签"):
                labels_add = [x.strip() for x in p[3:].strip(":： ").split(",") if x.strip()]
            elif p.startswith("减标签"):
                labels_del = [x.strip() for x in p[3:].strip(":： ").split(",") if x.strip()]
            elif p.startswith("好感度变化"):
                try:
                    favor_delta = int(p.split(":")[-1].strip())
                except ValueError:
                    pass
        self.store.update_person(qq, name=name, labels_add=labels_add,
                                 labels_del=labels_del, favor_delta=favor_delta)

    def _log_known_qqs(self) -> set:
        """今日会话日志里出现过的所有 QQ 号（(QQ:数字) 格式）。"""
        path = self.log_dir / f"{datetime.now():%Y%m%d}.log"
        if not path.exists():
            return set()
        text = path.read_text(encoding="utf-8", errors="replace")
        return set(re.findall(r"QQ:(\d+)", text))

    # ---------------- 组装 ----------------

    def _load_persona_meta(self) -> dict:
        personas_dir = self._path("personas_dir", "../personas")
        meta = personas_dir / self.cfg["persona"]["slug"] / "meta.json"
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _favor_of(self, qq: str) -> float:
        """某人的好感度（供情绪拉力加权；没有记录的人视为 0）。"""
        if self.store is None:
            return 0.0
        person = self.store.data.get("people", {}).get(str(qq)) or {}
        try:
            return float(person.get("favor", 0))
        except (TypeError, ValueError):
            return 0.0

    def _build_system_prompt(self) -> str:
        personas_dir = self._path("personas_dir", "../personas")
        sp = personas_dir / self.cfg["persona"]["slug"] / "system_prompt.md"
        prompt = sp.read_text(encoding="utf-8")
        prompt = _STRIP_FENCE.sub("", prompt.strip())
        owner = str(self.cfg["persona"].get("owner_qq", "") or "").strip()
        if owner:
            prompt += (
                f"\n\n【大人】QQ 号是 {owner}。来者是不是大人，以此为准；"
                f"其余账号皆非大人。"
            )
        if self.store is not None:
            mem_text, self._mention_map = self.store.numbered_memories()
            if mem_text:
                prompt += "\n\n" + mem_text
            ppl = self.store.format_people_for_prompt()
            if ppl:
                prompt += "\n\n" + ppl
        return prompt

    def _build_emoji_hint(self) -> str:
        """表情关键词注入段：上机时构建一次、逐条决策复用（控 token）。

        表情库为空时返回空串——决策提示词与旧版完全一致，零额外成本；
        增删图片后重启生效（与索引刷新时机一致）。
        """
        kws = self.emoji.keywords(limit=EMOJI_HINT_MAX_KEYWORDS)
        if not kws:
            return ""
        return EMOJI_HINT_TEMPLATE.format(keywords="、".join(kws))

    def _log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        with open(self.log_dir / f"{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
