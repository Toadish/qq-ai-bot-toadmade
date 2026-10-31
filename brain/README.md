# brain/ — 聊天大脑（多 LLM · OpenAI 兼容）

机器人"灵魂"：OneBot 报文 → 记忆注入 → LLM 决策 → 限流发送 → 日志与总结。

## 文件

| 文件 | 作用 |
|---|---|
| `brain.py` | 总指挥 `ChatBrain`：上机/下机、消息处理、是否回复决策（群聊两步决策+沉默因子）、记忆注入、"话费"提醒、表情选择 |
| `llm.py` | OpenAI 兼容客户端（多服务商预设：deepseek/openai/anthropic/gemini/openrouter/custom；密钥走 `llm.api_key_env` 环境变量） |
| `mood.py` | 两级情绪引擎（总心境 + 按人分化，喜怒哀惧；长期情绪、好感度加权牵引、在线/离线衰减） |
| `proactive.py` | 主动行为（无聊/倾诉事件、群聊/私聊/说说加权渠道、发送通道清单） |
| `onebot.py` | OneBot v11 报文解析/构造（分段数组 ↔ 文本、send_msg 动作、群聊回复默认不带 @，`mention=True` 预留给主动话题） |
| `timesource.py` | 时间源（事件时间 → 联网北京时间兜底，防改系统时钟） |
| `usage.py` | token 用量记账（"话费"：日预算、八成提醒一次、余额不足兜底） |
| `emoji.py` | 表情包库（`images/emoji` 关键词命中 + `images/collected` 主动搜罗） |
| `tests/` | 离线测试（假 LLM，不需要 API Key） |

## 消息处理流程（每条消息）

```
OneBot 事件 → 过滤(meta/自己的) → 分段转文本 → 搜罗图片入库
  → 短期记忆 add_turn → 写会话日志（带 QQ）
  → LLM 决策（system=人设+编号回忆+人物+时间参考+表情词表；
     输出 JSON {reply, text, emoji, mentioned}）
  → 提起加分（mentioned 编号 → mark_mentioned，每天每条限一次）
  → 回复 = 是 → 表情（emoji 联想 → 文本关键词兜底）→ send_msg → 限流发送 → 记日志
```

## "话费不够"机制

- 每天累计 token（DeepSeek 返回的 `usage.total_tokens`），存在 `brain/usage.json`；
- 超过 `llm.daily_budget_tokens × llm.warn_ratio`（默认 20 万 × 0.8）时，
  当天**只提醒一次**：下一次回复里角色会自然地说一句"盘缠/话费不够"；
- 余额真的不足（HTTP 402）：不发 LLM 调用，直接用角色台词兜底回复一次：
  "大人……我这边的「盘缠」用尽了，暂时说不了话。改日大人续上，我们再谈。"

## 表情包

- 投放：把图片放进 `../images/emoji/`，**文件名即关键词**（"猫猫-开心.png"）；
- 双通道挑图：① **LLM 情绪联想**——决策 JSON 的 `emoji` 字段按情绪自选
  （悲伤→流泪、厌烦→吐舌头……），关键词表注入提示词（封顶 12 个、空库零成本）；
  ② **文本关键词兜底**——回复文本命中文件名关键词才发；
- 搜罗：聊天中收到的图片自动收藏进 `../images/collected/`（本地/file:///http(s)
  三种来源，哈希去重、限量 300 张、单张 ≤10MB）；
- 节奏：每 4 条回复最多带一次表情，两次表情至少间隔 30 秒（防风控）。

## 测试

```powershell
cd brain\tests
..\..\websocket\.venv\Scripts\python.exe test_brain.py
```

## 上线前检查清单

1. `$env:DEEPSEEK_API_KEY = "sk-..."`（环境变量，别写进 config.json）；
2. `images/emoji/` 里放几张表情（可后补）；
3. 运行 `..\websocket\launcher.py`——大脑随桥接自动上机/下机，无需单独启动。

## 已知取舍（v1）

- 决策与回复合并为**一次 LLM 调用**（JSON 模式），省 token；长回复不流式。
- 收到图片只标记为 `[图片]`，暂不做 OCR/视觉理解（DeepSeek 的视觉模型后续可接）。
- 短期记忆压缩是同步调用（每 10 轮多几秒延迟），见 memory/README.md 红线 2。
