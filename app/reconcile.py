"""振动记录仪计数与纸本 UTC 时刻的核对算法。

计数规则：每毫秒加一，满 MOD 归零；相邻事件间隔 1..MAX_GAP 毫秒。
对相邻事件 (prev, cur)，计数差（模 MOD）记为 r in [0, MOD-1]，
则真实间隔 d 必须满足 d = r + k*MOD 且 1 <= d <= MAX_GAP：

* r == 0 时只能取 MOD（固定间隔）；
* 1 <= r <= MAX_GAP - MOD 时有两个候选：r 与 r+MOD（模糊间隔）；
* 其余 r 只能取 r（固定间隔）。

因此任意两事件 i<j 之间的总耗时集合是以 low 为首、low+amb*MOD 为末、
MOD 为步长的等差数列，其中：

* low = 区间内所有间隔最小候选之和；
* amb = 区间内模糊间隔的条数。

核对无需重读整份事件流：导入时为每个事件预算前缀和 P[k]、模糊计数 A[k]，
核对任意事件对只需两次按事件号的点查。
"""

from __future__ import annotations

MOD = 65536
MAX_GAP = 90000
AMBIGUOUS_LIMIT = MAX_GAP - MOD  # r <= 24464 时间隔模糊
STEP = MOD
MIN_ANCHORS = 3


def interval_options(count_prev: int, count_cur: int) -> tuple[int, int, bool]:
    """返回相邻事件间隔的 (最小候选, 最大候选, 是否模糊)。"""
    r = (count_cur - count_prev) % MOD
    if r == 0:
        # k=0 会得到间隔 0，非法；只能绕一圈
        return MOD, MOD, False
    low = r
    high = r + MOD if r <= AMBIGUOUS_LIMIT else r
    return low, high, high != low


def analyze_counts(counts: list[int]) -> list[dict]:
    """按事件顺序为每个事件预算前缀量。

    返回 [{"event_no", "count", "prefix_min", "prefix_amb"}]，
    prefix_min/prefix_amb 统计事件 1 到当前事件之间的间隔。
    """
    records: list[dict] = []
    prefix_min = 0
    prefix_amb = 0
    prev: int | None = None
    for idx, count in enumerate(counts, start=1):
        if not isinstance(count, int) or not 0 <= count <= MOD - 1:
            raise ValueError(f"事件 {idx} 的计数越界: {count!r}")
        if prev is not None:
            low, _high, ambiguous = interval_options(prev, count)
            prefix_min += low
            if ambiguous:
                prefix_amb += 1
        records.append(
            {
                "event_no": idx,
                "count": count,
                "prefix_min": prefix_min,
                "prefix_amb": prefix_amb,
            }
        )
        prev = count
    return records


def range_between(records: dict[int, tuple[int, int]], i: int, j: int) -> tuple[int, int]:
    """事件 i 到事件 j（i<j）可达总耗时的 (最小值, 最大值)。

    records: {event_no: (prefix_min, prefix_amb)}
    """
    if i >= j:
        raise ValueError("range_between 要求 i < j")
    pmin_i, pamb_i = records[i]
    pmin_j, pamb_j = records[j]
    low = pmin_j - pmin_i
    amb = pamb_j - pamb_i
    return low, low + amb * MOD


def pair_ok(
    records: dict[int, tuple[int, int]],
    event_i: int,
    utc_i: int,
    event_j: int,
    utc_j: int,
) -> tuple[bool, int, int]:
    """核对一对锚点，返回 (是否一致, 可达最小耗时, 可达最大耗时)。"""
    low, high = range_between(records, event_i, event_j)
    delta = utc_j - utc_i
    ok = low <= delta <= high and (delta - low) % MOD == 0
    return ok, low, high


def _clamp_progression(low: int, high: int) -> tuple[int, int] | None:
    """把等差数列（步长 MOD，low 为首项）截断到 UTC 毫秒 >= 0。"""
    if low > high:
        return None
    if low < 0:
        # 向上取整到同余的非负项
        low = low + ((-low + MOD - 1) // MOD) * MOD
    if low > high:
        return None
    return low, high


def candidate_range(
    records: dict[int, tuple[int, int]],
    target_event: int,
    neighbors: tuple[tuple[int, int] | None, tuple[int, int] | None],
) -> dict | None:
    """求某个锚点单独可替换的时刻区间。

    neighbors = (前一锚点 (event_no, utc_ms) 或 None,
                 后一锚点 (event_no, utc_ms) 或 None)
    返回 {"first", "last", "step", "count"} 或 None（无可行值）。
    """
    prev_anchor, next_anchor = neighbors
    bounds: list[tuple[int, int]] = []

    if prev_anchor is not None:
        ev, utc = prev_anchor
        low, high = range_between(records, ev, target_event)
        # target 在 prev 之后：t ∈ [utc+low, utc+high]
        bounds.append((utc + low, utc + high))
    if next_anchor is not None:
        ev, utc = next_anchor
        low, high = range_between(records, target_event, ev)
        # target 在 next 之前：t ∈ [utc-high, utc-low]
        bounds.append((utc - high, utc - low))

    if not bounds:
        return None

    low = max(b[0] for b in bounds)
    high = min(b[1] for b in bounds)
    if len(bounds) == 2:
        # 两条等差数列必须同余才能相交出可行值
        if (bounds[0][0] - bounds[1][0]) % MOD != 0:
            return None

    clamped = _clamp_progression(low, high)
    if clamped is None:
        return None
    first, last = clamped
    return {
        "first": first,
        "last": last,
        "step": STEP,
        "count": (last - first) // STEP + 1,
    }


def verify(
    records: dict[int, tuple[int, int]],
    anchors: list[dict],
) -> dict:
    """按当前全部锚点核对全账。

    anchors: [{"event_no", "utc_ms", "id"?}]，事件号互不相同且均在账内。
    返回核对结论；不一致时给出所有“只改其时刻即可对平”的锚点候选区间。
    """
    anchors = sorted(anchors, key=lambda a: a["event_no"])
    if len(anchors) < MIN_ANCHORS:
        return {
            "status": "insufficient",
            "message": f"至少需要 {MIN_ANCHORS} 条锚点，当前 {len(anchors)} 条",
            "pairs": [],
            "candidates": [],
        }

    pairs: list[dict] = []
    all_ok = True
    for left, right in zip(anchors, anchors[1:]):
        ok, low, high = pair_ok(
            records,
            left["event_no"], left["utc_ms"],
            right["event_no"], right["utc_ms"],
        )
        pairs.append(
            {
                "left_event": left["event_no"],
                "right_event": right["event_no"],
                "delta": right["utc_ms"] - left["utc_ms"],
                "low": low,
                "high": high,
                "step": STEP,
                "ok": ok,
            }
        )
        all_ok = all_ok and ok

    if all_ok:
        return {
            "status": "consistent",
            "message": "全部对得上，账纸一致",
            "pairs": pairs,
            "candidates": [],
        }

    # 逐锚点假设“只有它抄错”：其余锚点的相邻对必须全部一致
    candidates: list[dict] = []
    for idx, anchor in enumerate(anchors):
        prev_anchor = anchors[idx - 1] if idx > 0 else None
        next_anchor = anchors[idx + 1] if idx < len(anchors) - 1 else None

        remaining_pairs_ok = True
        for left, right in zip(anchors, anchors[1:]):
            if left["event_no"] == anchor["event_no"] or right["event_no"] == anchor["event_no"]:
                continue  # 跳过所有涉及待改锚点的对
            ok, _low, _high = pair_ok(
                records,
                left["event_no"], left["utc_ms"],
                right["event_no"], right["utc_ms"],
            )
            if not ok:
                remaining_pairs_ok = False
                break
        if not remaining_pairs_ok:
            continue

        rng = candidate_range(
            records,
            anchor["event_no"],
            (
                (prev_anchor["event_no"], prev_anchor["utc_ms"]) if prev_anchor else None,
                (next_anchor["event_no"], next_anchor["utc_ms"]) if next_anchor else None,
            ),
        )
        if rng is None:
            continue
        candidates.append(
            {
                "anchor_id": anchor.get("id"),
                "event_no": anchor["event_no"],
                "original_utc_ms": anchor["utc_ms"],
                **rng,
            }
        )

    if not candidates:
        message = "单处更正无法对平：请核对是否有两处及以上锚点抄错"
    else:
        message = f"发现 {len(candidates)} 个可通过单处更正对平的锚点"
    return {
        "status": "inconsistent",
        "message": message,
        "pairs": pairs,
        "candidates": candidates,
    }
