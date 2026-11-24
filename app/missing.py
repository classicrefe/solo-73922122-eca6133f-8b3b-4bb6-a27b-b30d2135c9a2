"""漏导核算：在“已导事件”之间推算最少漏导事件数。

业务前提
========
* 选段由两条已核实锚点界定，时间跨度至多 ``MAX_SPAN_MS``（30 分钟），
  段内已导事件至多 ``MAX_IMPORTED_EVENTS``（200）条；
* 段内全部锚点视为准确，相邻锚点把整段切成若干子段，各子段独立核算；
* 保留原事件的顺序与计数，只允许在**相邻原事件之间**插入漏导事件，
  计数回绕（MOD=65536）与相邻事件实际间隔（1..MAX_GAP 毫秒）规则不变。

单间隔建模
==========
对相邻已导事件（计数 c1 -> c2），模差 r=(c2-c1) mod MOD，最小候选 b：

* r=0：b=MOD（不允许漏导时间隔为 0）；
* r>0：b=r；模糊间隔（r<=24464）的小间隔另有 b+MOD 候选。

插入 m 条漏导后共有 m+1 个同余小间隔，总耗时 d=b+k·MOD（k=回绕圈数），
可实现的充要条件：

* m+1 >= k（k 个 +MOD 候选各占一个小间隔，每个至多 1 个）；
* (m+1)·b <= d <= (m+1)·MAX_GAP。

固定间隔也能靠漏导承载回绕圈，只是 m=0 时 k 必须为 0。

子段核算（动态规划）
====================
子段含 n 个相邻已导事件间隔，锚点时差 D 必须满足
D = B + K·MOD（B=各间隔最小候选之和，K=回绕圈数）。
以“已用回绕圈数”为状态做 DP：逐间隔枚举圈数 k，由
``_missing_for_turns`` 直接给出该间隔最小漏导 m，求全段漏导总数最小
的安排并回溯；同成本时取圈数序列字典序最小者，结果确定。
任何子段无解时整段判为“即使允许漏导也对不上”，提示核查锚点或计数。
"""

from __future__ import annotations

import math

from .reconcile import MAX_GAP, MOD, interval_options

MAX_SPAN_MS = 30 * 60 * 1000  # 选段时间跨度上限：30 分钟
MAX_IMPORTED_EVENTS = 200    # 选段内已导事件条数上限
MIN_SEGMENT_ANCHORS = 2      # 漏导核算至少两条锚点（两端）


class MissingError(ValueError):
    """选段本身不满足核算前提（区别于“对不上”的 impossible 结论）。"""


def missing_for_elapsed(elapsed_ms: int) -> int:
    """单个相邻已导事件间隔给定实际耗时，所需的最少漏导条数。"""
    if elapsed_ms <= 0:
        raise ValueError("间隔耗时必须为正")
    return max(0, math.ceil(elapsed_ms / MAX_GAP) - 1)


def _interval_profile(count_prev: int, count_cur: int) -> dict:
    """相邻已导事件间隔的静态画像：模差、最小候选、是否模糊。"""
    residue = (count_cur - count_prev) % MOD
    low, _high, ambiguous = interval_options(count_prev, count_cur)
    return {
        "residue": residue,
        "base": low,
        "ambiguous": ambiguous,
    }


def _missing_for_turns(profile: dict, turns: int) -> int | None:
    """给定间隔的额外回绕圈数 turns，返回最少漏导数；不可行返回 None。

    插入 m 条漏导后共有 m+1 个相邻小间隔，每个模差同为 r，候选为
    b 与 b+MOD（仅“模糊间隔”有 +MOD 候选）。总耗时 d=b+turns*MOD
    可实现的充要条件：

    * turns 个 +MOD 要分配到 m+1 个小间隔（每个至多 1 个），
      即 m+1 >= turns，亦即 m >= turns-1；固定间隔也能靠漏导满足，
      只是 m=0 时 turns 必须为 0；
    * 每个小间隔 <= MAX_GAP，即 d <= (m+1)*MAX_GAP；
    * 下界 (m+1)*b <= d（小间隔最小均为 b）。
    """
    if turns < 0:
        return None
    base = profile["base"]
    d = base + turns * MOD
    m = max(0, turns - 1)
    # 放大小间隔数量，直到每个小间隔都不超过 MAX_GAP 且下界成立
    while (m + 1) * MAX_GAP < d or (m + 1) * base > d:
        m += 1
    return m


def _solve_subsegment(
    profiles: list[dict], elapsed: int, max_elapsed: int | None = None
) -> dict | None:
    """求单个锚点对子段的最少漏导安排。

    DP 状态 dp[k]：处理完若干间隔、恰用 k 个额外回绕圈时的最优
    (漏导总数, 选择序列)。每个间隔只枚举 0..剩余圈数，漏导数由
    _missing_for_turns 直接给出。并列时优先靠前间隔少用圈，
    使安排确定且贴近原始无漏导时间线。
    返回 {"missing", "extra_turns", "intervals":[...]}；无解返回 None。
    """
    base_sum = sum(p["base"] for p in profiles)
    if elapsed < base_sum or (elapsed - base_sum) % MOD != 0:
        return None
    if max_elapsed is not None and elapsed > max_elapsed:
        return None
    total_turns = (elapsed - base_sum) // MOD

    # 每项为 (最小漏导数, 圈数序列, 各间隔 (圈数, 漏导, 耗时) 序列) 或 None。
    # 同一“已用圈数”格内：先比漏导总成本，成本相同则取圈数序列字典序最小者
    # （靠前间隔尽量不加圈，安排最贴近无漏导原时间线，且结果确定）。
    dp: list[tuple[int, tuple[int, ...], tuple] | None] = [None] * (total_turns + 1)
    dp[0] = (0, (), ())

    for profile in profiles:
        nxt: list[tuple[int, tuple[int, ...], tuple] | None] = [None] * (total_turns + 1)
        for used_prev, state in enumerate(dp):
            if state is None:
                continue
            prev_cost, prev_turns, prev_seq = state
            for ki in range(total_turns - used_prev + 1):
                mi = _missing_for_turns(profile, ki)
                if mi is None:
                    continue
                d = profile["base"] + ki * MOD
                used = used_prev + ki
                turn_seq = prev_turns + (ki,)
                candidate = (prev_cost + mi, turn_seq, prev_seq + ((ki, mi, d),))
                if nxt[used] is None or (candidate[0], candidate[1]) < (
                    nxt[used][0],
                    nxt[used][1],
                ):
                    nxt[used] = candidate
        dp = nxt

    if dp[total_turns] is None:
        return None

    total_missing, _turns, seq = dp[total_turns]
    intervals: list[dict] = []
    for idx, profile in enumerate(profiles):
        k, m, d = seq[idx]
        intervals.append(
            {
                "residue": profile["residue"],
                "base_ms": profile["base"],
                "ambiguous": profile["ambiguous"],
                "extra_turn": k,
                "elapsed_ms": d,
                "missing": m,
            }
        )
    return {
        "missing": total_missing,
        "extra_turns": total_turns,
        "base_ms": base_sum,
        "intervals": intervals,
    }


def compute_missing(events: list[dict], anchors: list[dict]) -> dict:
    """对选段核算最少漏导。

    events: 选段内已导事件，按事件号连续排列，
            每项 {"event_no", "count"}；
    anchors: 段内全部锚点（视为准确），每项至少含
             {"event_no", "utc_ms"}，事件号互不相同且落在 events 范围内。

    返回结论字典；子段无法解释时 status="impossible"，不抛异常。
    选段结构性问题（锚点不足、事件号逆序等）抛 MissingError。
    """
    if len(events) < 2:
        raise MissingError("选段至少需要 2 条已导事件")
    event_nos = [ev["event_no"] for ev in events]
    expected = event_nos[0]
    for no in event_nos:
        if no != expected:
            raise MissingError("选段事件号必须连续")
        expected += 1
    count_by_event = {ev["event_no"]: ev["count"] for ev in events}

    ordered = sorted(anchors, key=lambda a: a["event_no"])
    if len(ordered) < MIN_SEGMENT_ANCHORS:
        raise MissingError(f"漏导核算至少需要 {MIN_SEGMENT_ANCHORS} 条段内锚点")
    if len({a["event_no"] for a in ordered}) != len(ordered):
        raise MissingError("锚点事件号重复")
    for anchor in ordered:
        if anchor["event_no"] not in count_by_event:
            raise MissingError(f"锚点事件 {anchor['event_no']} 不在选段内")
        if anchor["utc_ms"] < 0:
            raise MissingError("UTC 毫秒不能为负")

    left_event = ordered[0]["event_no"]
    right_event = ordered[-1]["event_no"]
    left_utc = ordered[0]["utc_ms"]
    right_utc = ordered[-1]["utc_ms"]
    span_ms = right_utc - left_utc
    # 负时差不在此拒绝：它属于“即使允许漏导也对不上”的核算结论（impossible），
    # 交由子段求解返回；这里只拦截正向超跨度。
    if span_ms > MAX_SPAN_MS:
        raise MissingError(
            f"选段时间跨度 {span_ms}ms 超过上限 {MAX_SPAN_MS}ms（30 分钟）"
        )
    imported_event_count = right_event - left_event + 1
    if imported_event_count > MAX_IMPORTED_EVENTS:
        raise MissingError(
            f"选段内已导事件 {imported_event_count} 条，超过上限 {MAX_IMPORTED_EVENTS} 条"
        )

    selection = {
        "left_event": left_event,
        "right_event": right_event,
        "left_utc_ms": left_utc,
        "right_utc_ms": right_utc,
        "span_ms": span_ms,
        "imported_event_count": imported_event_count,
        "anchor_count": len(ordered),
        "anchors": [
            {
                "anchor_id": a.get("id"),
                "event_no": a["event_no"],
                "utc_ms": a["utc_ms"],
            }
            for a in ordered
        ],
    }

    subsegments: list[dict] = []
    total_missing = 0
    impossible_pair: dict | None = None

    for left_anchor, right_anchor in zip(ordered, ordered[1:]):
        ev_l, ev_r = left_anchor["event_no"], right_anchor["event_no"]
        delta = right_anchor["utc_ms"] - left_anchor["utc_ms"]
        profiles = [
            _interval_profile(count_by_event[no], count_by_event[no + 1])
            for no in range(ev_l, ev_r)
        ]
        base_sum = sum(p["base"] for p in profiles)
        solved = _solve_subsegment(profiles, delta, max_elapsed=MAX_SPAN_MS)

        sub: dict = {
            "left_event": ev_l,
            "right_event": ev_r,
            "elapsed_ms": delta,
            "base_ms": base_sum,
        }
        if solved is None:
            if delta < 0:
                reason = "negative_delta"
                hint = "锚点时刻逆序（时差为负）"
            elif delta < base_sum:
                reason = "below_minimum"
                hint = f"时差 {delta}ms 小于计数可达最小耗时 {base_sum}ms"
            elif (delta - base_sum) % MOD != 0:
                reason = "residue_mismatch"
                hint = (
                    f"时差与最小耗时之差 {delta - base_sum}ms 不是步长 {MOD} 的整数倍"
                )
            elif delta > MAX_SPAN_MS:
                reason = "span_exceeded"
                hint = f"子段耗时 {delta}ms 超过 30 分钟上限 {MAX_SPAN_MS}ms"
            else:
                reason = "not_realizable"
                hint = (
                    f"需要 {(delta - base_sum) // MOD} 个额外回绕圈，"
                    "但在相邻事件间隔 1..90000ms 规则下无法安排足够漏导事件承载"
                )
            sub.update({"ok": False, "reason": reason, "hint": hint})
            subsegments.append(sub)
            impossible_pair = sub
            break

        sub.update(
            {
                "ok": True,
                "missing": solved["missing"],
                "extra_turns": solved["extra_turns"],
                "intervals": [
                    {
                        "left_event": no,
                        "right_event": no + 1,
                        **solved["intervals"][offset],
                    }
                    for offset, no in enumerate(range(ev_l, ev_r))
                ],
            }
        )
        total_missing += solved["missing"]
        subsegments.append(sub)

    if impossible_pair is not None:
        return {
            "status": "impossible",
            "message": (
                f"即使允许漏导也对不上：锚点对事件 {impossible_pair['left_event']}→"
                f"{impossible_pair['right_event']} {impossible_pair['hint']}，"
                "请核查锚点或计数"
            ),
            "constraints": {
                "max_span_ms": MAX_SPAN_MS,
                "max_imported_events": MAX_IMPORTED_EVENTS,
                "max_gap_ms": MAX_GAP,
                "mod": MOD,
            },
            "selection": selection,
            "total_missing": None,
            "subsegments": subsegments,
        }

    return {
        "status": "feasible",
        "message": (
            f"最少漏导 {total_missing} 条即可解释整段"
            if total_missing
            else "无需漏导即可解释整段（最少漏导 0 条）"
        ),
        "constraints": {
            "max_span_ms": MAX_SPAN_MS,
            "max_imported_events": MAX_IMPORTED_EVENTS,
            "max_gap_ms": MAX_GAP,
            "mod": MOD,
        },
        "selection": selection,
        "total_missing": total_missing,
        "subsegments": subsegments,
    }
