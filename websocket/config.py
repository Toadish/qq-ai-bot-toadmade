# -*- coding: utf-8 -*-
"""
config.py — 配置加载器
======================
读取 config.json，与内置默认值合并后返回 dict。

优先级：命令行参数 > config.json > 内置默认值
（命令行的优先级由各入口脚本自行处理，本模块负责「文件 + 默认值」这一层。）

密钥约定：API Key 等敏感值一律走环境变量（如 DEEPSEEK_API_KEY），
config.json 里只记录「环境变量名」，避免密钥被提交进 Git。
"""

import json
from pathlib import Path

# 内置默认值：config.json 缺失或某个字段缺失时兜底
DEFAULTS: dict = {
    "server": {"host": "127.0.0.1", "port": 6701},
    # 发送限流：并发恒为 1，按字数加延迟 + 随机抖动，超长截断，失败指数退避（防腾讯风控）
    "send_guard": {
        "max_concurrency": 1,
        "base_delay_seconds": 1.0,
        "chars_per_second": 20.0,
        "max_message_chars": 300,          # 文本回复长度上限（JSON 报文不截断）
        "jitter_min": 0.8,                 # 延迟随机抖动范围（乘数）
        "jitter_max": 1.5,
        "pre_delay_min_seconds": 0.8,      # 前摇：发送前随机停顿下限（秒，模拟"想一下再回"）
        "pre_delay_max_seconds": 2.0,      # 前摇上限（秒）
        "failure_backoff_base_seconds": 2.0,   # 连续失败退避：base * 2^(n-1)
        "failure_backoff_max_seconds": 30.0,   # 退避封顶
        "failure_alarm_threshold": 5,          # 连续失败 N 次 → 告警
    },
    # 短期记忆（滑动窗口 + 压缩摘要），实现见 memory/short_term.py
    "short_term": {
        "window_turns": 12,           # 最近 N 轮保留原文
        "summary_chunk_turns": 10,    # 每 N 轮压成一条一级摘要
        "max_level1_summaries": 5,    # 一级摘要上限
        "level2_chunk_summaries": 4,  # 几条一级摘要压成一条二级
        "max_level2_summaries": 3,    # 二级摘要上限
        "summary_max_chars": 300,     # 单条摘要字符硬上限
        "context_budget_chars": 4000, # 注入提示词的总预算
    },
    # 聊天大脑，实现见 brain/brain.py
    "brain": {
        "max_reply_chars": 300,                 # 回复长度上限（与限流截断配合）
        "collect_received_images": True,        # 主动收藏聊天里收到的图片（"搜罗"表情）
        "collect_max_files": 300,               # 收藏上限（张）
        "collect_max_file_bytes": 10 * 1024 * 1024,  # 单张上限（字节）
        "emoji_min_interval_messages": 4,       # 每 N 条回复最多带一次表情
        "emoji_cooldown_seconds": 30,           # 两次表情之间的最短间隔（秒）
        "emoji_collected_chance": 0.05,         # 关键词未命中时随机用收藏图的概率
        "summary_max_chars": 16000,             # 下机总结时喂给 LLM 的日志最大长度
        "summary_min_log_bytes": 50,            # 日志小于该字节数则跳过总结
        "compress_max_tokens": 400,             # 短期记忆压缩单次调用的 max_tokens
        "group_silence_chance": 0.8,            # 群聊沉默因子（角色级：人设 meta.json 的 group_silence_chance 优先）
        "group_reply_blacklist": [],            # 群聊回复黑名单（QQ 号列表）：这些人的群消息直接跳过（如群友的 bot）
    },
    # 入群/加好友请求处理（实现见 brain/brain.py 的 _handle_request）
    "requests": {
        "mode": "off",              # off=仅记录不处理 | llm=交给角色决策（计入话费）| approve_all=全部同意 | refuse_all=全部拒绝
        "friend": True,             # 是否处理好友申请
        "group_add": True,          # 是否处理入群申请（sub_type=add）
        "group_invite": True,       # 是否处理入群邀请（sub_type=invite）
    },
    # NapCat 守护参数
    "napcat": {
        "access_token": "",
        "shell_dir": "..\\napcat\\NapCat.XXXX.Shell",  # 相对配置文件所在目录（部署时改）
        "boot_command": ["napcat_launcher.bat", "123456789"],  # 经 bat(cmd /c) 启动；带 QQ 号 = 快速登录（部署时改）
        "login_timeout_seconds": 120,   # 启动后多久没连上桥接算一次登录失败
        "restart_backoff_seconds": 5,   # 拉起间隔
        "max_login_failures": 3,        # 连续失败 N 次 → 熔断告警并停止
        "qr_delay_seconds": 10,         # 扫码模式：启动后多久自动打开二维码图片
        "fast_exit_hint_seconds": 10,   # 启动后 N 秒内退出 → 打印常见原因提示
    },
    # 运行路径（相对路径基于配置文件所在目录；{slug} 替换为当前人设目录名）
    "paths": {
        "images_dir": "../images",
        "personas_dir": "../personas",
        "memory_file": "../personas/{slug}/memory.json",
        "sessions_dir": "../personas/{slug}/sessions",
        "usage_file": "../brain/usage.json",
        "stm_state_file": "../brain/stm_state.json",
        "mood_file": "../brain/mood.json",
        "apikey_file": ".apikey",
        "alarm_log_file": "alarm.log",
        "alarm_marker_file": "ALARM.txt",
    },
    "llm": {
        # 服务商预设：deepseek | openai | anthropic | gemini | openrouter | custom。
        # 各预设的 base_url / model / api_key_env 见 brain/llm.py 的 PROVIDER_PRESETS，
        # 下面三个字段留空 = 用预设值，填了就覆盖预设（自定义端点最灵活）。
        "provider": "deepseek",
        "base_url": "",
        "model": "",
        "api_key_env": "",  # 从哪个环境变量读密钥（留空用预设）
        "max_tokens": 1024,
        "temperature": 0.7,          # 设 null 可省略该参数（部分推理模型不支持）
        "max_tokens_param": "max_tokens",  # OpenAI 新推理模型改 "max_completion_tokens"
        "timeout_seconds": 60,
        "daily_budget_tokens": 200000,  # 每日「话费」预算（token）
        "warn_ratio": 0.8,              # 用掉 80% 时提醒一次（由角色转达）
    },
    # 时间源（实现见 brain/timesource.py）：事件时间优先、联网北京时间兜底，
    # 生产路径绝不用本地电脑时间（防改系统时钟刷好感度/每日上限）
    "timesource": {
        "endpoints": [
            "https://api.m.taobao.com/rest/api3.do?api=mtop.common.getTimestamp",
            "https://timeapi.io/api/Time/current/zone?timeZone=Asia/Shanghai",
            "http://worldtimeapi.org/api/timezone/Asia/Shanghai",
        ],
        "timeout_seconds": 5,   # 单个端点超时（秒）
        "cache_seconds": 300,   # 联网结果缓存时长（秒）；事件时间同样按此新鲜度判定
    },
    # 情绪（实现见 brain/mood.py）：总情绪（心境）+ 按人分化，喜怒哀惧 0~100，
    # LLM 每次决策顺带报 delta（限幅），按时间源向基线衰减；
    # calm_factor 是平复因子（角色级，人设 meta.json 的 mood_calm_factor 优先）
    "mood": {
        "decay_per_hour": 5.0,     # 每维每小时向基线衰减的基准点数
        "calm_factor": 1.0,        # 平复因子：× decay_per_hour = 实际速度（<1 平复慢）
        "max_delta": 5.0,          # 单次消息的情绪变化上限
        "baseline": {"喜": 10, "怒": 5, "哀": 10, "惧": 15},  # 各维基线（她底子偏静）
        "influence_base_positive": 0.06,  # 喜 的拉力（/小时，分情绪 → 总心境）
        "influence_base_negative": 0.05,  # 怒/哀/惧 的拉力（/小时）
        "favor_scale": 0.08,       # 好感度每 +1：喜拉力 ×(1+s)、负面拉力 ×(1-s)
        "favor_clamp": 10.0,       # 好感度参与加权时的上下限
        "long_term_merge": 0.2,    # 下机时 长期情绪 ← 分情绪 的牵引速率
        "long_term_decay": 0.05,  # 下机时 长期情绪 → 基线 的自然回落速率（防只涨不跌）
    },
    # 主动发起话题 / 发朋友圈（实现见 brain/proactive.py）
    "proactive": {
        "enabled": True,                    # 总开关（临时关掉主动行为用）
        "tick_seconds": 60,                 # 后台检查间隔
        "idle_seconds": 300,                # 近 N 秒无收发消息才算"空闲"
        "boredom_cooldown_seconds": 600,    # 无聊事件冷却（每次尝试都计入）
        "boredom_chance": 0.15,             # 无聊时行动的随机因子（角色级：人设 meta.json 的 boredom_chance 优先）
        "boredom_joy_max": 20,              # 无聊判定：喜 ≤ 该值
        "boredom_sorrow_max": 20,           # 无聊判定：哀 ≤ 该值
        "vent_threshold": 60,               # 倾诉判定：任一心境 > 该值
        "vent_cooldown_seconds": 10800,     # 倾诉事件冷却（默认 3 小时）
        # 渠道权重（渠道候选来自长期记忆的发送通道清单：同意入群时建群通道、
        # 新建在意的人标签时建私聊通道）：说说权重调低、私聊调高
        "channel_weights": {"group": 1.0, "private": 1.5, "qzone": 0.5},
    },
    "persona": {
        "slug": "yotohime",   # 当前加载的人设（将来换 hatsune-miku 只改这里）
        "owner_qq": "",       # 大人的 QQ 号（注入 system prompt；请求决策据此识别大人）
    },
    "log_level": "INFO",
}


def _merge(base: dict, override: dict) -> dict:
    """两层浅合并：override 覆盖 base 的同名字段，base 独有的字段保留。"""
    merged = dict(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # "_说明" 之类的注释字段不参与合并
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict:
    """加载配置。path 省略时取本文件同目录下的 config.json。"""
    if path is None:
        path = Path(__file__).with_name("config.json")
    path = Path(path)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            file_cfg = json.load(f)
    else:
        print(f"[config] 未找到 {path}，使用内置默认值")
        file_cfg = {}
    return _merge(DEFAULTS, file_cfg)


def resolve_path(value: str, config_dir: Path, slug: str | None = None) -> Path:
    """把配置里的路径解析为绝对路径。

    - 相对路径基于配置文件所在目录（config_dir）解析；
    - {slug} 占位符会被替换为当前人设目录名（persona.slug）。
    """
    if slug:
        value = value.replace("{slug}", slug)
    p = Path(value)
    if not p.is_absolute():
        p = config_dir / p
    return p.resolve()
