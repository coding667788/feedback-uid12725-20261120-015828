"""领域规则：候选边合法性、连通分量、值守区间与覆盖分钟。

时间一律使用整数分钟（从某个虚构纪元起算），值守区间为半开区间 [start, end)，
即第 start 分钟当班、第 end 分钟已下班。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


class RuleError(ValueError):
    """规则表或输入声明不合法。"""


class ScheduleError(ValueError):
    """排班声明不合法。"""


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

def normalized_type_pair(type_a: str, type_b: str) -> tuple[str, str]:
    """类型对规范化：规则按无序类型对声明，存储与查询均用排序后的元组。"""
    return tuple(sorted((type_a, type_b)))  # type: ignore[return-value]


@dataclass(frozen=True)
class TypeRule:
    type_a: str
    type_b: str
    max_diff: int

    @property
    def pair(self) -> tuple[str, str]:
        return normalized_type_pair(self.type_a, self.type_b)


def build_rule_index(rules: list[dict]) -> dict[tuple[str, str], int]:
    """把输入声明的规则表转为 {规范化类型对: 最大时差} 索引并做合法性校验。"""
    index: dict[tuple[str, str], int] = {}
    for raw in rules:
        missing = {"type_a", "type_b", "max_diff"} - set(raw)
        if missing:
            raise RuleError(f"规则缺少字段 {sorted(missing)}: {raw}")
        type_a, type_b = str(raw["type_a"]), str(raw["type_b"])
        max_diff = raw["max_diff"]
        if not isinstance(max_diff, int) or isinstance(max_diff, bool) or max_diff < 0:
            raise RuleError(f"max_diff 必须为非负整数分钟: {raw}")
        if type_a == type_b:
            raise RuleError(f"规则类型对必须是两个不同类型: {raw}")
        pair = normalized_type_pair(type_a, type_b)
        if pair in index:
            raise RuleError(f"规则表中类型对 {pair} 重复声明")
        index[pair] = max_diff
    if not index:
        raise RuleError("规则表至少要声明一条类型对规则")
    return index


def edge_is_legal(
    *,
    channel_a: str,
    channel_b: str,
    site_a: str,
    site_b: str,
    area_a: str,
    area_b: str,
    type_a: str,
    type_b: str,
    minute_a: int,
    minute_b: int,
    rule_index: dict[tuple[str, str], int],
) -> bool:
    """判断两条告警是否构成合法跨通道候选边。

    条件（全部满足）：
      1. 通道不同（同通道告警之间永远不建边，只能经跨通路边间接归并）；
      2. 同站点、同安全区域；
      3. 类型对在规则表中声明允许；
      4. 整数分钟差不超过该类型对允许的最大时差（恰等也合法，使用 <=）。
    """
    if channel_a == channel_b:
        return False
    if site_a != site_b or area_a != area_b:
        return False
    pair = normalized_type_pair(type_a, type_b)
    max_diff = rule_index.get(pair)
    if max_diff is None:
        return False
    return abs(minute_a - minute_b) <= max_diff


# ---------------------------------------------------------------------------
# 连通分量
# ---------------------------------------------------------------------------

def connected_components(
    active_ids: list[str], adjacency: dict[str, set[str]]
) -> list[list[str]]:
    """在活动告警（未封存、非迟到）上按无向边求连通分量。

    返回按成员 ID 排序、整体按首个成员 ID 排序的分量列表，保证输出确定性。
    孤立点（未获关联的单条告警）同样形成一个单成员分量。
    """
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(active_ids):
        if start in seen:
            continue
        queue = deque([start])
        seen.add(start)
        members: list[str] = []
        while queue:
            node = queue.popleft()
            members.append(node)
            for nxt in sorted(adjacency.get(node, ())):
                if nxt not in seen and nxt in set(active_ids):
                    seen.add(nxt)
                    queue.append(nxt)
        components.append(sorted(members))
    components.sort(key=lambda members: members[0])
    return components


# ---------------------------------------------------------------------------
# 排班
# ---------------------------------------------------------------------------

def normalize_schedule_entry(entry: dict) -> dict:
    """校验并规范化一条排班。

    半开区间 [start_minute, end_minute)；cross_day=True 表示跨日班次，
    此时 end_minute 为次日相对零点的分钟，规范化为 end_minute + day_minutes。
    """
    missing = {"person", "group", "start_minute", "end_minute"} - set(entry)
    if missing:
        raise ScheduleError(f"排班缺少字段 {sorted(missing)}: {entry}")
    person = str(entry["person"])
    group = str(entry["group"])
    start = entry["start_minute"]
    end = entry["end_minute"]
    day_minutes = int(entry.get("day_minutes", 1440))
    cross_day = bool(entry.get("cross_day", False))
    ints_ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, day_minutes))
    if not ints_ok or day_minutes <= 0:
        raise ScheduleError(f"排班分钟字段必须为整数且 day_minutes>0: {entry}")
    if cross_day:
        # 跨日：start 是当日分钟，end 是次日分钟
        if not (0 <= start < day_minutes) or not (0 < end <= day_minutes):
            raise ScheduleError(f"跨日排班的分钟超出当日范围: {entry}")
        end_abs = end + day_minutes
    else:
        if not (0 <= start < end):
            raise ScheduleError(f"排班须满足 0 <= start < end: {entry}")
        end_abs = end
    return {
        "person": person,
        "group": group,
        "start": start,
        "end": end_abs,
    }


def groups_on_duty(entries: list[dict], minute: int) -> set[str]:
    """返回某一分钟当班的去重组集合（半开：start <= minute < end）。"""
    return {e["group"] for e in entries if e["start"] <= minute < e["end"]}


def persons_on_duty(entries: list[dict], minute: int) -> dict[str, str]:
    """返回某一分钟当班的 {人员: 组}（半开区间），重叠班按人去重。"""
    on_duty: dict[str, str] = {}
    for e in entries:
        if e["start"] <= minute < e["end"]:
            if e["person"] in on_duty and on_duty[e["person"]] != e["group"]:
                raise ScheduleError(
                    f"人员 {e['person']} 在第 {minute} 分钟同时属于不同组"
                )
            on_duty[e["person"]] = e["group"]
    return on_duty


def compute_deadline(
    entries: list[dict], from_minute: int, required_minutes: int = 30
) -> int | None:
    """从封存水位起累加 required_minutes 个双组覆盖分钟，返回审核截止时刻。

    覆盖分钟：该分钟至少有两名不同组人员当班；按去重分钟累计，不按人头重复。
    截止时刻取第 required_minutes 个覆盖分钟的“下一分钟”（半开语义：
    合法签发区间为 [watermark, deadline)）。覆盖不足返回 None（无可排期限）。
    """
    if not entries:
        return None
    horizon = max(e["end"] for e in entries)
    covered = 0
    minute = from_minute
    while minute < horizon:
        if len(groups_on_duty(entries, minute)) >= 2:
            covered += 1
            if covered == required_minutes:
                return minute + 1
        minute += 1
    return None


def count_coverage_minutes(
    entries: list[dict], from_minute: int, until_minute: int
) -> int:
    """统计 [from_minute, until_minute) 内的双组覆盖分钟数（供回执与核对）。"""
    count = 0
    for minute in range(from_minute, until_minute):
        if len(groups_on_duty(entries, minute)) >= 2:
            count += 1
    return count
