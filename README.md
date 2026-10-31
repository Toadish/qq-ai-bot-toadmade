# QQ AI Bot · qq-ai-bot-toadmade

**v2.0.0**

一个基于 [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot 11）与 Python 的
QQ 机器人框架：内置 LLM 角色扮演大脑、双层记忆系统、两级情绪引擎与主动行为调度。
换一个模型、换一个人设目录，就是另一个"活"的 QQ 机器人。

---

## 亮点功能

- **可换人设的角色扮演大脑**：一个角色 = 一个 `personas/<slug>/` 目录
  （系统提示词 + 角色级行为参数），换人设只改一处配置。
- **两级情绪引擎（喜怒哀惧）**：总体心境与按人分化情绪独立演化；人物层有
  长期情绪（多次相处才沉淀，"看见某个人就来气"）；情绪随好感度加权牵引心境；
  在线/离线两种衰减；情绪还会触发表情包联想。
- **双层记忆**：长期记忆（每次下机由角色总结关键回忆、每日遗忘仪式、
  珍贵回忆晋升、被准确提起时加分）＋ 短期记忆（滑动窗口 + 两级压缩摘要）。
- **主动行为调度**：角色闲下来会"无聊"、情绪高涨会"倾诉"——随机在
  群聊 / 私聊 / 空间说说中择一行动，渠道按权重抽取；群聊回复带**沉默因子**
  与**黑名单**（可屏蔽群里的其他机器人，杜绝互聊死循环）。
- **像真人的群聊表现**：选择性插话、一次只回一句（默认 20 字内）、不带 @、
  发送前随机"前摇"停顿 + 全局限流（防风控）。
- **入群 / 加好友请求处理**：off / 角色决策 / 全部同意 / 全部拒绝四种策略，
  决策失败保持待处理、绝不擅自放行陌生人。
- **时间感知（防作弊）**：优先采用消息事件时间，兜底联网北京时间，生产路径
  绝不读本地时钟——改系统时间刷不了好感度与每日上限。
- **合并转发阅读与空间说说**：能读懂群里转发的合并消息；能发布 QQ 空间说说。
- **多 LLM 接入（OpenAI 兼容）**：DeepSeek / OpenAI / Claude / Gemini /
  OpenRouter / 自托管（Ollama 等）一套代码通吃（见下文）。
- **"话费"预算**：每日 token 预算与用量记账，用超八成由角色提醒一次。

## 快速开始

两种安装方式任选其一。

### 方式一：一键安装器（推荐）

把以下四个文件放在**同一目录**，双击 `install.bat`：

| 文件 | 说明 |
|---|---|
| `install.bat` / `install.ps1` | 一键安装器 |
| `install_napcat.zip` | NapCat 一键安装包 + 本体包备份 |
| `qq-ai-bot-toadmade-2.0.0.zip` | 本工程源码包 |

安装器会按顺序自动完成：

1. 解压 `install_napcat.zip` 并改名为 `napcat`；
2. 解压源码包；
3. 解压 NapCat OneKey 安装包并启动 `NapCatInstaller.exe`（图形界面中一路
   下一步完成安装；若要求 QQ 路径可指向 `napcat\QQ.exe`，若询问 NapCat 本体
   包，同目录已备好 `NapCat.Shell.zip`）；
4. 按提示输入机器人 QQ 号后，自动生成 `onebot11_<QQ>.json` 与
   `napcat_launcher.bat`，并自动填写 `config.json` 的 `shell_dir` /
   `boot_command`（其余留给你手填）。

安装器结束后还剩三件手动的事：**扫码登录 QQ**（双击
`napcat\NapCat.*.Shell\napcat_launcher.bat`）、**准备人设目录**（见
「自定义人设」）、**配置 LLM 密钥**（见下）。

### 方式二：源码包手动安装

> 注意：**源码包不自带 NapCat**。此方式需要自行下载安装 NapCat 的
> OneKey 版本。

**1. 环境**

- Python 3.10+
- 依赖安装：`pip install -r requirements.txt`

**2. 安装 NapCat（OneKey 版）并接入本框架**

1. 从 [NapCat 发布页](https://github.com/NapNeko/NapCatQQ/releases) 下载
   OneKey 安装包并启动 `NapCatInstaller.exe` 完成安装、扫码登录一个 QQ 号；
2. 在 NapCat 目录的 `config/`（或 `versions/.../resources/app/napcat/config/`）
   下放置 OneBot 11 配置文件 `onebot11_<你的QQ号>.json`，内容参考：

```json
{
  "network": {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [
      {
        "name": "qq-ai-bot",
        "enable": true,
        "url": "ws://127.0.0.1:6701",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 3000,
        "token": ""
      }
    ],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": true,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
```

> `url` 的端口要与本工程 `websocket/config.json` 里 `server.port`（默认 6701）
> 一致；`parseMultMsg: true` 让合并转发消息附带解析内容。

**3. 准备人设目录**

本仓库不带人设。按 `docs/persona.md` 的规范创建 `personas/<slug>/`：
至少需要 `meta.json` 与 `system_prompt.md` 两个文件。

**4. 修改配置 `websocket/config.json`**

| 必改项 | 说明 |
|---|---|
| `persona.slug` | 你的人设目录名 |
| `persona.owner_qq` | 主人的 QQ 号（注入提示词，用于请求决策等） |
| `llm.provider` | 模型服务商预设，见「多 LLM 接入」 |
| `napcat.shell_dir` / `boot_command` | 需要 launcher 守护 NapCat 时填（见「配置说明」） |

**5. 准备 API Key**

- 方式一：设置环境变量（如 `DEEPSEEK_API_KEY`，名字随 `llm.api_key_env`）；
- 方式二：把密钥写入 `websocket/.apikey`。**首次运行 launcher 时会自动引导**。

**6. 运行**

```powershell
python websocket/launcher.py
```

`launcher.py` = 桥接服务 + NapCat 守护（崩溃自动拉起）+ 登录熔断告警 + 首次运行引导。
只想跑桥接逻辑（调试用）可以：

```powershell
python websocket/server.py
```

看到 `[WsServerChannel] 监听 ws://127.0.0.1:6701` 且 NapCat 显示已连接，即可开始聊天。

## 配置说明（`websocket/config.json`）

| 配置组 | 作用 |
|---|---|
| `server` | 反向 WS 监听地址与端口 |
| `send_guard` | 发送限流：前摇停顿、发后间隔、超长截断、失败退避告警 |
| `short_term` | 短期记忆：窗口大小、压缩摘要参数、上下文预算 |
| `brain` | 回复长度上限、表情节奏、群聊沉默因子、群聊回复黑名单 |
| `requests` | 入群/加好友请求处理策略与开关 |
| `napcat` | NapCat 守护：启动命令、登录超时、熔断阈值 |
| `paths` | 所有运行路径（人设目录、记忆文件、日志目录等，支持 `{slug}` 占位符） |
| `llm` | LLM 服务商与参数（见下） |
| `timesource` | 北京时间源端点列表（按顺序降级） |
| `mood` | 情绪参数：基线、衰减速度、拉力系数、长期情绪混合比例 |
| `proactive` | 主动行为：空闲判定、冷却、随机因子、渠道权重 |
| `persona` | 当前人设 slug 与主人 QQ |

## 多 LLM 接入（OpenAI 兼容）

所有受支持的服务商都提供 OpenAI 兼容的 `/chat/completions` 接口，改
`llm.provider` 即可切换（`base_url` / `model` / `api_key_env` 留空用预设、
填写则覆盖）：

| provider | base_url | 建议 model | 环境变量 |
|---|---|---|---|
| `deepseek` | https://api.deepseek.com | deepseek-chat | DEEPSEEK_API_KEY |
| `openai` | https://api.openai.com/v1 | gpt-4.1-mini | OPENAI_API_KEY |
| `anthropic` | https://api.anthropic.com/v1 | claude-sonnet-4-5 | ANTHROPIC_API_KEY |
| `gemini` | https://generativelanguage.googleapis.com/v1beta/openai | gemini-2.5-flash | GEMINI_API_KEY |
| `openrouter` | https://openrouter.ai/api/v1 | deepseek/deepseek-chat | OPENROUTER_API_KEY |
| `custom` | 任意（Ollama / vLLM / LM Studio…） | 任意 | LLM_API_KEY |

```json
"llm": {
  "provider": "gemini",
  "model": "",
  "daily_budget_tokens": 1000000,
  "warn_ratio": 0.8
}
```

注意事项：

- 各家密钥前缀不同（`sk-` / `sk-ant-` / `AIza…`），框架只要求非空，不校验前缀；
- OpenAI 新一代推理模型需把 `max_tokens_param` 改为 `max_completion_tokens`，
  不支持 `temperature` 的模型可将其设为 `null` 省略；
- `response_format: json_object` 不一定被所有兼容端点强制执行——大脑侧对
  非 JSON 输出有纯文本回退，不影响使用。

## 自定义人设

人设目录规范、`meta.json` 字段（含角色级行为参数：平复因子、无聊因子、
群聊沉默因子）与制作步骤见 **[docs/persona.md](docs/persona.md)**。

## 运行测试

全部测试离线可跑（假 LLM / 假时钟，不需要 API Key）：

```powershell
python websocket/tests/test_limiter.py      # 发送限流与前摇
python websocket/tests/test_channel_rpc.py  # 通道 API 调用（echo 关联）
python brain/tests/test_timesource.py       # 时间源解析与降级
python brain/tests/test_llm.py              # LLM 客户端：预设/请求体/重试
python brain/tests/test_mood.py             # 两级情绪引擎
python brain/tests/test_proactive.py        # 主动行为事件
python brain/tests/test_brain.py            # 大脑全流程
python brain/tests/test_requests.py         # 入群/加好友请求处理
python memory/test_memory.py                # 长期记忆引擎
python memory/test_short_term.py            # 短期记忆引擎
```

## 目录结构

```
qq-ai-bot-toadmade/
├── README.md                # 本文件
├── requirements.txt
├── docs/
│   └── persona.md           # 人设目录规范
├── brain/                   # 聊天大脑
│   ├── brain.py             # 主脑：上机/下机、消息处理、决策、总结
│   ├── llm.py               # OpenAI 兼容 LLM 客户端（多服务商预设）
│   ├── mood.py              # 两级情绪引擎（喜怒哀惧）
│   ├── proactive.py         # 主动行为（无聊/倾诉事件、渠道加权）
│   ├── onebot.py            # OneBot 报文解析/构造
│   ├── timesource.py        # 时间源（事件时间→联网北京时间）
│   ├── emoji.py             # 表情包库
│   ├── usage.py             # token 记账（"话费"）
│   └── tests/               # 大脑侧离线测试
├── memory/                  # 双层记忆
│   ├── memory.py            # 长期记忆（遗忘仪式/珍贵回忆/人物层）
│   ├── short_term.py        # 短期记忆（窗口+压缩摘要）
│   ├── SKILL.md             # 记忆机制规格（规则/稳态数学/调参）
│   └── test_*.py
└── websocket/               # 接入层
    ├── launcher.py          # 一键上机（桥接+NapCat 守护+熔断+引导）
    ├── server.py            # 仅桥接（调试用）
    ├── client.py            # 正向 WS 客户端 CLI
    ├── echo_server.py       # WS 回显服务器（通道联调用）
    ├── channel.py           # WS 通道抽象（正/反向 + API RPC）
    ├── config.py            # 配置默认值（与 config.json 两层合并）
    ├── config.json          # 运行配置
    └── tests/
```

## 适配你的角色：brain.py 里的提示词

`brain/brain.py` 顶部的提示词常量默认面向**偏安静、话少**的角色，且措辞带示例
人设的语境（例如余额提醒台词里的「大人」「盘缠」）。换角色时建议通读并按需
改写：

| 常量 | 用途 |
|---|---|
| `DECISION_INSTRUCTION` | 私聊决策指令（是否回复、极简回复、JSON 字段约定） |
| `GROUP_DECISION_INSTRUCTION` | 群聊决策指令（是否想回、情绪变化与提起识别） |
| `GROUP_REPLY_INSTRUCTION` | 群聊回复生成指令（20 字内、句尾省略号等） |
| `REQUEST_DECISION_INSTRUCTION` | 入群 / 加好友请求的决策指令 |
| `BALANCE_LINE` / `REMINDER` | 余额不足台词与"话费"提醒（示例人设词汇最集中的两处） |
| `SUMMARY_TEMPLATE` | 下机总结指令（回忆提炼规则） |

若你的角色话多、措辞现代，把"尽量 20 字以内""句尾常用省略号"与专属词汇换成
角色的语言即可；`max_reply_chars` 等硬性限制在 config 里调。

## 常见问题

| 现象 | 处理 |
|---|---|
| NapCat 连不上 | 检查 onebot11 配置的 `url` 端口与 `server.port` 是否一致 |
| 日志提示「时间不可用」 | 换 `timesource.endpoints`（网络受限时自建一个北京时间接口即可） |
| 余额不足提醒 | LLM 返回 HTTP 402 时角色会用台词兜底；充值即可恢复 |
| 群聊回复太频繁 | 调高人设 meta.json 的 `group_silence_chance`（0~1） |
| 群里被其他机器人缠上 | 把它的 QQ 号加进 `brain.group_reply_blacklist` |
| 主动行为太频繁 | 调大 `proactive.boredom_cooldown_seconds` 或调低 `boredom_chance` |
| 想换模型 | 只改 `llm.provider`（+对应环境变量密钥），其余不动 |

## 合规与风控提示

QQ 平台对机器人行为有风控与使用规范。本框架已内置发送限流与人性化节奏，
但请仅在平台规则允许的范围内、以学习或个人低频使用为目的运行；请遵守当地
法律法规，并对机器人产出的内容负责。

## 许可证与声明

本作品**为爱发电、免费开放**：任何人可自由使用、修改与分享，无意商用，
不附加任何限制（亦不提供任何保证）。若你基于某商业角色制作人设，请注意
角色版权归属，仅作个人自用。

## 版本历史

### v2.0.0

- 两级情绪引擎：总心境 + 按人分化 + 长期情绪，好感度加权牵引、在线/离线双衰减
- 主动行为调度：无聊/倾诉事件、群聊/私聊/空间说说加权渠道、发送通道清单入记忆
- 群聊治理：两步决策 + 沉默因子、群聊回复黑名单、极简回复、不带 @
- 入群/加好友请求处理（四种策略 + 角色决策）
- 合并转发阅读、空间说说发布、时间感知防作弊、每日遗忘仪式与提起加分
- 发送前摇 + 限流、token 日预算
- 多 LLM OpenAI 兼容接入（DeepSeek / OpenAI / Claude / Gemini / OpenRouter / 自定义）

### v1.0.0

- NapCat（OneBot 11）+ 自定义 Python 接入层 + LLM 聊天大脑
- 双层记忆系统、表情包库、"话费"记账、守护与登录熔断
