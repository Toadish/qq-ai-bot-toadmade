# 人设目录规范（persona）

一个角色 = `personas/<slug>/` 目录。换人设只需替换目录内容，并在
`websocket/config.json` 的 `persona.slug` 里指向它。

## 目录结构

```
personas/
└── <slug>/                 # 目录名即 slug（config 的 persona.slug）
    ├── meta.json           # 必填：角色元信息 + 角色级行为参数
    ├── system_prompt.md    # 必填：系统提示词（注入每次 LLM 调用的 system 位）
    ├── persona.md          # 可选：人设建模资料（仅作参考，不注入）
    ├── character.md        # 可选：同上
    ├── world.md            # 可选：世界观资料
    ├── dialogue_samples.md # 可选：对白样本
    ├── sessions/           # 运行时自动生成：会话日志（按天）
    └── memory.json         # 运行时自动生成：长期记忆数据
```

## meta.json 必填/常用字段

| 字段 | 说明 |
|---|---|
| `slug` | 目录名，保持一致 |
| `display_name` | 角色自称（大脑落款、日志署名） |

其余字段自由扩展（如 `source`、`cv`、`weapon` 等设定资料），运行时只读取
下列行为参数。

## 角色级行为参数（meta.json）

这些字段只在人设的 meta.json 里定义，运行时**优先于 config 的同名参数**
（config 里的同名项只是兜底默认值）。0 也是合法值。

| 字段 | 建议范围 | 含义与调法 |
|---|---|---|
| `mood_calm_factor` | 0.2 ~ 2.0 | 平复因子：情绪衰减速度 = `mood.decay_per_hour` × 本值。<1 时久久不能平复（情绪浓烈的角色），>1 时很快归于平静 |
| `boredom_chance` | 0 ~ 1 | 无聊事件随机因子：闲下来"想开口"的概率。沉默寡言的角色设低（如 0.1~0.2），话痨设高 |
| `group_silence_chance` | 0 ~ 1 | 群聊沉默因子：LLM 判"想回"后、真正开口的概率只有 1−本值。偏安静的角色设高（0.7~0.9） |

示例：

```json
{
  "slug": "my-character",
  "display_name": "我的角色",
  "mood_calm_factor": 1.0,
  "boredom_chance": 0.15,
  "group_silence_chance": 0.8
}
```

## system_prompt.md 写作要点

- 写清楚**说话风格**（句式长短、口头禅、称呼习惯）；
- 写清楚**互动边界**（对陌生人/群聊/请求的态度），决策层会据此判断；
- 框架会自动注入【当前时间】、【你此刻的心境】、【你此刻对他的情绪】、
  【你记得的人】（含好感度与长期情绪）、编号回忆与主人的QQ（= config 的
  `persona.owner_qq`）——提示词里不必重复这些，但可以约定角色**如何使用**
  它们（例如"时间词只在话题相关时使用"）。

> 除 system_prompt 外，`brain/brain.py` 顶部的提示词常量（决策指令、余额台词
> 等）默认面向偏安静的角色、且带示例人设的词汇（如「盘缠」）——换角色时需
> 同步改写，详见根 README「适配你的角色：brain.py 里的提示词」。

## 制作步骤

1. 复制一份目录为 `personas/<新 slug>/`；
2. 写 `system_prompt.md`；
3. 在 `meta.json` 里按角色性格设置三个行为参数；
4. 修改 `websocket/config.json`：`persona.slug` → 新目录名、
   `persona.owner_qq` → 主人的 QQ 号；
5. 重启 launcher 生效（`memory.json` / `sessions/` 会自动为新角色创建）。
