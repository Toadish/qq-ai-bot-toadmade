# -*- coding: utf-8 -*-
"""
test_memory.py — 长期记忆引擎仿真测试
======================================
运行：python test_memory.py（或 .venv\Scripts\python.exe test_memory.py）

覆盖：字节截断 / 每会话上限 / 每日遗忘仪式（同日不重复）/ 计数与晋升 /
      珍贵遗忘 / 提起加分（每天每条限一次、跨重启持久化）/ 时间不可用兜底 /
      稳态（普通≈50、珍贵≈10）/ 人物层 / 持久化。
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from memory import MemoryStore  # noqa: E402


class FakeClock:
    """测试用假时间源：从 2026-10-29 08:00 开始，可逐日推进。"""

    def __init__(self):
        self._dt = datetime(2026, 10, 29, 8, 0, 0)

    def __call__(self):
        return self._dt

    def advance(self, days=1):
        self._dt += timedelta(days=days)


def new_store(**params) -> MemoryStore:
    """临时目录里建一个空存储（测试互不干扰）。默认注入假时钟。"""
    has_clock = "clock" in params
    clock = params.pop("clock", None)
    if clock is None and not has_clock:
        clock = FakeClock()
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    return MemoryStore(Path(tmp) / "memory.json", params=params, seed=42, clock=clock)


def t_truncate_utf8():
    store = new_store()
    n = store.session_end(["妖" * 60])  # 60 个汉字 = 180 字节 > 128
    assert n == 1
    mem = store.data["ordinary"][0]
    b = len(mem["text"].encode("utf-8"))
    assert b <= 128, f"截断后仍有 {b} 字节"
    print(f"PASS 字节截断：180B → {b}B，未切断多字节字符")


def t_max_new_per_session():
    store = new_store()
    n = store.session_end([f"回忆{i}" for i in range(15)])
    assert n == 10, f"应为 10，实际 {n}"
    print("PASS 每次下机最多 10 条")


def t_count_and_promotion():
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0, precious_threshold=15)
    store.session_end(["一起看过雨"])
    mid = store.data["ordinary"][0]["id"]
    # 15 天：每天第一次上机执行遗忘仪式，存活计数 +1，第 15 天达到阈值 → 晋升
    for _ in range(15):
        clock.advance(1)
        snap = store.session_start()
    assert not snap["ordinary"], "晋升后普通回忆应为空"
    assert len(snap["precious"]) == 1, f"珍贵回忆应为 1，实际 {len(snap['precious'])}"
    assert snap["precious"][0]["count"] == 15
    assert snap["precious"][0]["id"] == mid
    print("PASS 计数与晋升：15 天（每天一次仪式）后晋升为珍贵回忆")


def t_same_day_startup_no_repeat():
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0)
    store.session_start()        # 第 1 天仪式（库是空的）
    store.session_end(["同一天"])  # 加入一条，count=0
    store.session_start()        # 同日再次上机：仪式跳过，count 不变
    assert store.data["ordinary"][0]["count"] == 0
    assert store.data["meta"]["last_forget_date"] == str(clock().date())
    clock.advance(1)
    store.session_start()        # 新的一天：仪式执行，count 0→1
    assert store.data["ordinary"][0]["count"] == 1
    print("PASS 每日遗忘仪式：同日多次上机不重复遗忘/计数")


def t_cross_midnight_shutdown_catchup():
    # 连续运行跨天没重启：下机时补做当天的遗忘仪式
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0, precious_threshold=1)
    store.session_start()                     # 第 1 天仪式
    store.session_end(["跨天兜底"])             # 第 1 天下机：写入
    clock.advance(1)
    n = store.session_end(["第二天的新回忆"])     # 跨天未重启：下机补做当日仪式
    assert store.data["meta"]["last_forget_date"] == str(clock().date())
    assert n == 1
    assert any(m["text"] == "跨天兜底" for m in store.data["precious"]), \
        "补做的仪式应完成遗忘+计数+晋升"
    print("PASS 跨天兜底：未重启时下机补做当日遗忘仪式")


def t_precious_decay():
    # 每次晋升都对既有珍贵回忆按 100% 遗忘 → 珍贵层永远只有最新一条
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0, precious_forget_rate=1.0,
                      precious_threshold=1)
    store.session_start()   # 第 1 天仪式（空）
    store.session_end(["第一段"])
    store.session_end(["第二段"])
    clock.advance(1)
    snap = store.session_start()   # 第 2 天仪式：count+1 → 阈值 1 → 依次晋升
    assert len(snap["precious"]) == 1
    assert snap["precious"][0]["text"] == "第二段"
    print("PASS 珍贵遗忘：新晋者幸存，旧者被遗忘")


def t_mention_once_per_day():
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0)
    store.session_end(["那杯没喝完的茶"])
    mid = store.data["ordinary"][0]["id"]
    n1 = store.mark_mentioned([mid, mid, mid])  # 同一天重复提起
    assert n1 == 1, f"同一天只应 +1，实际 {n1}"
    assert store.data["ordinary"][0]["count"] == 1
    n2 = store.mark_mentioned([mid])
    assert n2 == 0, "同日再提起不应加分"
    clock.advance(1)
    n3 = store.mark_mentioned([mid])
    assert n3 == 1, "新的一天应可再次加分"
    assert store.data["ordinary"][0]["count"] == 2
    print("PASS 提起加分：每天每条限一次，隔天恢复")


def t_mention_persists_across_restart():
    clock = FakeClock()
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    path = Path(tmp) / "memory.json"
    s1 = MemoryStore(path, seed=1, clock=clock)
    s1.session_end(["重启也不许刷分"])
    mid = s1.data["ordinary"][0]["id"]
    assert s1.mark_mentioned([mid]) == 1
    s2 = MemoryStore(path, seed=2, clock=clock)   # 模拟同日重启
    assert s2.mark_mentioned([mid]) == 0, "重启后同一天仍应被限次"
    clock.advance(1)
    assert s2.mark_mentioned([mid]) == 1, "隔天恢复"
    print("PASS 提起加分限次跨重启持久化（重启刷不了分）")


def t_no_clock_skips_daily():
    store = new_store(clock=None, ordinary_forget_rate=0.0)
    store.session_end(["没有时间源"])
    store.session_start()   # 时间不可用 → 跳过仪式
    assert store.data["ordinary"][0]["count"] == 0
    assert store.mark_mentioned([store.data["ordinary"][0]["id"]]) == 0
    print("PASS 时间不可用：跳过遗忘仪式与提起加分（宁可不做，不用本地时间）")


def t_steady_state_ordinary():
    # 稳态模拟：200 天，每天新增 10 条，遗忘率 0.2 → 普通回忆应稳定在 ~50
    clock = FakeClock()
    store = new_store(clock=clock)
    for i in range(200):
        clock.advance(1)
        store.session_start()
        store.session_end([f"第{i}天的第{j}条回忆" for j in range(10)])
    n = len(store.data["ordinary"])
    assert 25 <= n <= 90, f"稳态应在 50 附近，实际 {n}"
    print(f"PASS 普通回忆稳态 ≈ 50（实测 {n}，含晋升流失）")


def t_steady_state_precious():
    # 每天晋升 10 条新珍贵，旧珍贵遗忘率 0.1 → 珍贵层稳态 ≈ 10
    clock = FakeClock()
    store = new_store(clock=clock, ordinary_forget_rate=0.0, precious_forget_rate=0.1,
                      precious_threshold=1)
    for i in range(100):
        clock.advance(1)
        store.session_start()   # 每日仪式触发晋升（阈值 1）
        store.session_end([f"第{i}天的珍贵事件{j}" for j in range(10)])
    n = len(store.data["precious"])
    assert 3 <= n <= 25, f"珍贵稳态应在 10 附近，实际 {n}"
    print(f"PASS 珍贵回忆稳态 ≈ 10（实测 {n}）")


def t_numbered_memories():
    store = new_store()
    store.session_end(["第一条回忆", "第二条回忆"])
    text, mapping = store.numbered_memories()
    assert "[1] 第一条回忆" in text and "[2] 第二条回忆" in text
    assert mapping["1"] == store.data["ordinary"][0]["id"]
    assert mapping["2"] == store.data["ordinary"][1]["id"]
    empty = new_store()
    text2, mapping2 = empty.numbered_memories()
    assert text2 == "" and mapping2 == {}
    print("PASS 编号回忆注入：编号→id 映射、空库为空块")


def t_long_term_mood_merge():
    store = new_store()
    store.update_person("1001", name="某人", favor_delta=2)
    base = {"喜": 10, "怒": 5, "哀": 10, "惧": 15}
    # 第一次：分情绪 怒60 → 60 + 0.05×(5-60) = 57.25（也带向基线的回落）
    store.update_person_mood("1001", {"怒": 60, "喜": 20},
                             alpha=0.2, beta=0.05, baseline=base)
    assert store.data["people"]["1001"]["mood"]["怒"] == 57.25
    # 第二次：怒80 → 57.25 + 0.2×(80-57.25) + 0.05×(5-57.25) = 59.19
    store.update_person_mood("1001", {"怒": 80, "喜": 20},
                             alpha=0.2, beta=0.05, baseline=base)
    assert abs(store.data["people"]["1001"]["mood"]["怒"] - 59.19) < 0.01
    # 久不出现：连续平静会话 → 长期情绪单调滑回基线，不会只涨不跌
    for _ in range(10):
        store.update_person_mood("1001", {"怒": 5, "喜": 10},
                                 alpha=0.2, beta=0.05, baseline=base)
    assert store.data["people"]["1001"]["mood"]["怒"] < 10, "平静期应滑回基线附近"
    assert "长期情绪" in store.format_people_for_prompt()
    print("PASS 长期情绪：双向牵引——反复出现才升高、久不出现滑回基线")


def t_people_layer():
    store = new_store()
    store.update_person("1001", name="深夜访客", labels_add=["熟客"], favor_delta=2)
    store.update_person("1001", labels_add=["话少"], favor_delta=-1)
    store.update_person("1001", labels_del=["话少"])
    p = store.data["people"]["1001"]
    assert p["labels"] == ["熟客"]
    assert p["favor"] == 1
    assert "深夜访客" in store.format_for_prompt()
    assert "QQ 1001" in store.format_for_prompt()   # 人物层注入带 QQ 号（防编造）
    store.remove_person("1001")
    assert "1001" not in store.data["people"]
    print("PASS 人物层：标签增删、好感度加减、删除、注入文本")


def t_persistence():
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    path = Path(tmp) / "memory.json"
    s1 = MemoryStore(path, seed=1, clock=FakeClock())
    s1.session_end(["持久化测试"])
    s1.update_person("1002", labels_add=["测试"], favor_delta=5)
    s2 = MemoryStore(path, seed=2, clock=FakeClock())  # 重新加载
    assert s2.data["ordinary"][0]["text"] == "持久化测试"
    assert s2.data["people"]["1002"]["favor"] == 5
    print("PASS 持久化：落盘后重载一致")


def t_channel_lists():
    # 新建在意的人标签 → 自动建立私聊通道；群通道手动建立；旧数据回填
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    path = Path(tmp) / "memory.json"
    # 旧数据（无 channels 段，有人物层）→ 回填私聊通道
    MemoryStore(path, seed=1, clock=FakeClock()).update_person("2001", name="旧友")
    s = MemoryStore(path, seed=2, clock=FakeClock())
    assert s.data["channels"]["private"] == ["2001"], "旧人物层应回填为私聊通道"
    # 新人物标签 → 通道自动建立
    s.update_person("2002", name="新友")
    assert "2002" in s.data["channels"]["private"]
    # 已有人物更新 → 不重复
    s.update_person("2002", favor_delta=1)
    assert s.data["channels"]["private"].count("2002") == 1
    # 群通道：建立/去重/读取
    s.note_group_channel("888")
    s.note_group_channel("888")
    s.note_group_channel("777")
    groups, privates = s.channel_lists()
    assert groups == ["777", "888"] and "2001" in privates
    s2 = MemoryStore(path, seed=3, clock=FakeClock())
    assert s2.channel_lists()[0] == ["777", "888"], "群通道应持久化"
    print("PASS 发送通道清单：人物建档建私聊通道、群通道建/去重/持久化、旧数据回填")


if __name__ == "__main__":
    for fn in [
        t_truncate_utf8, t_max_new_per_session, t_count_and_promotion,
        t_same_day_startup_no_repeat, t_cross_midnight_shutdown_catchup,
        t_precious_decay, t_mention_once_per_day,
        t_mention_persists_across_restart, t_no_clock_skips_daily,
        t_steady_state_ordinary, t_steady_state_precious,
        t_people_layer, t_persistence, t_numbered_memories,
        t_long_term_mood_merge, t_channel_lists,
    ]:
        fn()
    print("\n全部测试通过 ✅")
