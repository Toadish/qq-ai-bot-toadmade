# memory/ — 记忆系统（长期 + 短期）

`SKILL.md` 是**机制规格**（规则、稳态数学、提示词模板、调参指南）；
本文件是**工程注意事项**与目录索引。

## 文件

| 文件 | 作用 |
|---|---|
| `memory.py` | 长期记忆引擎 `MemoryStore`（遗忘/计数/晋升/珍贵回忆/人物层） |
| `short_term.py` | 短期记忆 `ShortTermMemory`（滑动窗口 + 两级压缩摘要） |
| `SKILL.md` | 记忆系统 skill 规格（AgentSkills 格式） |
| `test_memory.py` / `test_short_term.py` | 仿真测试（稳态、晋升、截断、预算裁剪等） |

数据文件：`personas/<slug>/memory.json`（长期记忆，含可调参数）；
短期记忆运行期在内存，需要崩溃恢复时用 `save()/load()` 存档。

## 测试

```powershell
cd memory
..\websocket\.venv\Scripts\python.exe test_memory.py
..\websocket\.venv\Scripts\python.exe test_short_term.py
```

## ⚠️ 注意事项（红线）

1. **下机总结必须基于完整原始聊天日志**，不能只喂短期记忆的摘要。
   机器人应在每次「上机 → 下机」区间内单独留存完整日志（如
   `personas/<slug>/sessions/YYYYMMDD.log`），下机时把【完整日志】交给
   LLM 按 SKILL.md 第五节模板总结。摘要是有损压缩，用它做长期记忆原料
   会让回忆逐代失真——第一代丢 30% 细节，第二代就开始编造。
2. **压缩调用是同步的**：`ShortTermMemory.add_turn()` 在窗口溢出时会同步
   等待 LLM 压缩（每 10 轮多 2~5 秒延迟）。当前简单可靠优先；将来觉得卡
   再改成后台异步压缩（注意要做"压缩未完成时的兜底"）。
3. **调参只改数据文件**：长期记忆参数在 `personas/<slug>/memory.json`
   的 `params`；短期记忆参数在 `config.json` 的 `short_term` 组。
   引擎与数据格式保持稳定，别改代码。
4. **遗忘不可逆**：`session_start()` 的 20% 遗忘是直接删除，没有回收站。
   实验期可以先用副本文件跑，或者把 `ordinary_forget_rate` 临时调 0。
