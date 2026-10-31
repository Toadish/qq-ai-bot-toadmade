# -*- coding: utf-8 -*-
"""
test_mood.py — 按人分化的情绪状态离线测试
==========================================
运行：..\..\websocket\.venv\Scripts\python.exe test_mood.py

覆盖：基线初始化 / delta 限幅与 0~100 边界 / 时间衰减回基线 /
      按人隔离与称呼更新 / 持久化 / 时间不可用跳过衰减。
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

# GBK 控制台（Windows 默认）打印 ✅ 会崩；统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from brain.mood import MoodStore  # noqa: E402


class FakeClock:
    def __init__(self):
        self.dt = datetime(2026, 10, 30, 8, 0, 0)

    def __call__(self):
        return self.dt

    def advance(self, hours=1):
        self.dt += timedelta(hours=hours)


def new_store(clock=None):
    tmp = tempfile.mkdtemp(prefix="mood_test_")
    return MoodStore(Path(tmp) / "mood.json", clock=clock)


def t_baseline_and_format():
    store = new_store(FakeClock())
    line = store.line_for("111")
    assert line == "喜10、怒5、哀10、惧15", f"基线行不符：{line}"
    assert store.global_line() == "喜10、怒5、哀10、惧15", "总情绪基线应与默认基线一致"
    print("PASS 基线：总情绪与按人分化初始为各维基线，注入行格式正确")


def t_global_isolated():
    store = new_store(FakeClock())
    store.apply_global_delta({"哀": 3})
    store.apply_delta("111", "某人", {"怒": 3})
    assert store.global_line() == "喜10、怒5、哀13、惧15"
    # 新联系人继承总体心境（哀 13），再加怒 3 → 怒 8
    assert store.line_for("111") == "喜10、怒8、哀13、惧15"
    print("PASS 总情绪独立：心境与按人分化互不干扰（新联系人继承心境）")


def t_calm_factor():
    clock = FakeClock()
    fast = MoodStore(Path(tempfile.mkdtemp()) / "mood.json", clock=clock,
                     decay_per_hour=5.0, calm_factor=1.0)
    slow = MoodStore(Path(tempfile.mkdtemp()) / "mood.json", clock=clock,
                     decay_per_hour=5.0, calm_factor=0.5)
    for s in (fast, slow):
        for _ in range(4):
            s.apply_delta("111", None, {"怒": 5})   # 怒 5 → 25
    clock.advance(1)                                  # 1 小时
    # 快：衰减 5 → 20；慢：衰减 2.5 → 22.5
    assert fast.line_for("111").startswith("喜10、怒20"), fast.line_for("111")
    assert slow.line_for("111").startswith("喜10、怒22"), slow.line_for("111")
    print("PASS 平复因子：calm_factor 0.5 的衰减速度减半（角色级可调）")


def t_delta_clamp_and_bounds():
    store = new_store(FakeClock())
    store.apply_delta("111", "某人", {"怒": 99})       # 超限幅 → 只加 max_delta
    assert store.data["111"]["mood"]["怒"] == 10.0
    store.apply_delta("111", None, {"怒": -999})       # 负向超限 → 只减 max_delta
    assert store.data["111"]["mood"]["怒"] == 5.0
    store.apply_delta("111", None, {"惧": 5000})       # 限幅先于边界：只 +5
    assert store.data["111"]["mood"]["惧"] == 20.0
    for _ in range(20):                                # 连加冲到 100 封顶
        store.apply_delta("111", None, {"惧": 5})
    assert store.data["111"]["mood"]["惧"] == 100.0
    store.apply_delta("111", None, {"惧": -5000})      # 负向超限 → 只 -5
    assert store.data["111"]["mood"]["惧"] == 95.0
    for _ in range(20):                                # 连减压到 0 兜底
        store.apply_delta("111", None, {"惧": -5})
    assert store.data["111"]["mood"]["惧"] == 0.0
    store.apply_delta("111", None, {"不存在的维度": 99})  # 未知维度忽略
    print("PASS 限幅与边界：单次 ±max_delta、0~100、未知维度忽略")


def t_decay_toward_baseline():
    clock = FakeClock()
    store = new_store(clock)
    store.apply_delta("111", None, {"怒": 5, "喜": 5})   # 怒 10、喜 15（基线 怒5 喜10）
    clock.advance(2)                                     # 2 小时
    line = store.line_for("111")
    # 怒：10 → 向基线 5 衰减 5*2=10 → 5；喜：15 → 向基线 10 衰减 → 10
    assert "怒5" in line and "喜10" in line, f"衰减不符：{line}"
    clock.advance(100)                                   # 再久也只是回到基线
    line = store.line_for("111")
    assert "怒5" in line and "喜10" in line
    print("PASS 衰减：随时间线性滑回各维基线，且不低于基线")


def t_per_person_isolation():
    store = new_store(FakeClock())
    store.apply_delta("111", "甲", {"怒": 3})
    store.apply_delta("222", "乙", {"喜": -2})
    assert store.data["111"]["mood"]["怒"] == 8.0
    assert store.data["222"]["mood"]["怒"] == 5.0     # 乙不受甲影响
    assert store.data["222"]["mood"]["喜"] == 8.0
    assert store.data["111"]["name"] == "甲"
    print("PASS 按人隔离：各 QQ 独立存储、称呼随消息更新")


def t_persistence():
    clock = FakeClock()
    tmp = tempfile.mkdtemp(prefix="mood_test_")
    path = Path(tmp) / "mood.json"
    s1 = MoodStore(path, clock=clock)
    s1.apply_delta("111", "某人", {"哀": 4})
    s1.apply_global_delta({"惧": 5})
    s2 = MoodStore(path, clock=clock)   # 重新加载
    assert s2.data["111"]["mood"]["哀"] == 14.0
    assert s2.global_line() == "喜10、怒5、哀10、惧20"
    print("PASS 持久化：按人与总情绪落盘后重载一致")


def t_no_clock_skips_decay():
    store = new_store(clock=None)
    store.apply_delta("111", None, {"怒": 5})   # 时间不可用：只加减、不衰减
    assert store.data["111"]["mood"]["怒"] == 10.0
    assert store.line_for("111") == "喜10、怒10、哀10、惧15"
    print("PASS 时间不可用：跳过衰减，delta 照常落账")


def t_new_contact_inherits_global():
    store = new_store(FakeClock())
    for _ in range(6):
        store.apply_global_delta({"哀": 5})   # 心境哀 10 → 40（单次限幅 ±5）
    line = store.line_for("999")              # 没有记录的新人 → 继承总体心境
    assert line.startswith("喜10、怒5、哀40"), f"新联系人应继承总体心境：{line}"
    print("PASS 新联系人继承总体心境")


def t_startup_inheritance():
    clock = FakeClock()
    store = new_store(clock)
    store.apply_delta("111", None, {"怒": 5})          # 昨天留下点分情绪
    store.apply_delta("222", None, {"惧": 5})          # 无记录的人留下的旧值
    people = {"111": {"mood": {"喜": 20, "怒": 60, "哀": 10, "惧": 15},
                      "name": "甲"}}
    store.init_from_people(people)
    assert store.line_for("111").startswith("喜20、怒60")   # 有记录 → 继承长期情绪
    g = store.global_line()
    assert store.line_for("222") == g                     # 无记录 → 重置为总体心境
    print("PASS 上机继承：有记录继承长期情绪、无记录重置为总体心境")


def _seeded_store(clock, favor_map, online=True):
    """直接注入情绪数据：全局=基线，各联系人分情绪喜/怒=90，好感度映射。"""
    store = MoodStore(Path(tempfile.mkdtemp()) / "mood.json", clock=clock,
                      favor_of=lambda qq: favor_map.get(qq, 0.0))
    stamp = clock().isoformat(timespec="seconds")
    base = dict(store.baseline)
    store.data["_global"] = {"name": None, "mood": dict(base), "updated": stamp}
    for qq in favor_map:
        store.data[qq] = {"name": None,
                          "mood": {"喜": 90.0, "怒": 90.0, "哀": 10.0, "惧": 15.0},
                          "updated": stamp}
    store.data["_state"] = {"online": bool(online), "at": stamp}
    return store


def t_favor_weighted_pull():
    clock = FakeClock()
    high = _seeded_store(clock, {"111": 10})    # 好感度高
    low = _seeded_store(clock, {"111": -10})    # 好感度低
    clock.advance(1)
    high_line, low_line = high.global_line(), low.global_line()
    # 好感度高 → 喜拉力强（心境喜更高）、负面拉力弱（心境怒更低）
    hi_xi = int(high_line.split("、")[0].replace("喜", ""))
    lo_xi = int(low_line.split("、")[0].replace("喜", ""))
    hi_nu = int(high_line.split("、")[1].replace("怒", ""))
    lo_nu = int(low_line.split("、")[1].replace("怒", ""))
    assert hi_xi > lo_xi, f"喜：好感度高的拉力应更强 {high_line} vs {low_line}"
    assert hi_nu < lo_nu, f"怒：好感度高的拉力应更弱 {high_line} vs {low_line}"
    print("PASS 好感度加权：喜随好感增强、怒随好感减弱（四因子生效）")


def t_offline_natural_only():
    clock = FakeClock()
    store = _seeded_store(clock, {"111": -10}, online=False)   # 离线 + 高怒联系人
    clock.advance(1)
    g = store.global_line()
    assert g.startswith("喜10、怒5"), f"离线时不受任何人牵引：{g}"
    # 分情绪自身仍随时间自然衰减
    p = store.line_for("111")
    assert "怒8" in p or "怒7" in p or "怒6" in p, f"分情绪应自然衰减：{p}"
    print("PASS 离线纯自然衰减：总心境不被联系人牵引，分情绪照常衰减")


def t_online_pull_and_offline_release():
    clock = FakeClock()
    store = _seeded_store(clock, {"111": -10}, online=True)
    clock.advance(1)
    g1 = store.global_line()          # 在线 1 小时：怒被拉高
    assert int(g1.split("、")[1].replace("怒", "")) > 5
    store.set_online(False)
    clock.advance(1)
    g2 = store.global_line()          # 离线 1 小时：只自然回基线
    assert int(g2.split("、")[1].replace("怒", "")) < int(g1.split("、")[1].replace("怒", ""))
    print("PASS 在线/离线切换：在线带牵引、离线释放")


if __name__ == "__main__":
    t_baseline_and_format()
    t_global_isolated()
    t_calm_factor()
    t_delta_clamp_and_bounds()
    t_decay_toward_baseline()
    t_per_person_isolation()
    t_new_contact_inherits_global()
    t_startup_inheritance()
    t_favor_weighted_pull()
    t_offline_natural_only()
    t_online_pull_and_offline_release()
    t_persistence()
    t_no_clock_skips_decay()
    print("\n全部测试通过 ✅")
