"""独立小样本逐分钟参考实现（不导入任何生产代码）。

该文件刻意与 adjudicator/domain.py 分开编写：只用最朴素的字典/列表逐分钟扫描，
作为期限计算的独立预言机（oracle）。测试用它与生产实现双向核对。

约定与生产实现一致：
- 排班为半开区间 [start, end)；
- 覆盖分钟 = 该分钟当班人员至少来自两个不同组；按去重分钟计数，不按人头累计；
- 截止时刻 = 第 required 个覆盖分钟的下一分钟；不足则 None（无可排期限）。
"""
from __future__ import annotations


def ref_groups_at(entries, minute):
    """参考：逐分钟扫描所有区间，收集当班的组（集合自动去重）。"""
    groups = set()
    for e in entries:
        if e["start_minute"] <= minute < e["end_minute"]:
            groups.add(e["group"])
    return groups


def ref_deadline(entries, from_minute, required=30):
    """参考期限：从 from_minute 起逐分钟扫描，没有任何区间跳跃优化。"""
    if not entries:
        return None, []
    horizon = max(e["end_minute"] for e in entries)
    covered = 0
    minute = from_minute
    timeline = []  # 逐分钟轨迹，供测试断言
    while minute < horizon:
        groups = ref_groups_at(entries, minute)
        is_coverage = len(groups) >= 2
        timeline.append(
            {
                "minute": minute,
                "groups": sorted(groups),
                "coverage": is_coverage,
                "covered_after": covered + (1 if is_coverage else 0),
            }
        )
        if is_coverage:
            covered += 1
            if covered == required:
                return minute + 1, timeline
        minute += 1
    return None, timeline


def ref_coverage_count(entries, lo, hi):
    """参考：统计 [lo, hi) 内覆盖分钟数。"""
    count = 0
    for minute in range(lo, hi):
        if len(ref_groups_at(entries, minute)) >= 2:
            count += 1
    return count
