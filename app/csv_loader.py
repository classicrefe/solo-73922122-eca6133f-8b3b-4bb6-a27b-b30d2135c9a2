"""解析“按事件顺序排列”的 CSV：两列（事件号、计数）。

支持带表头（中文/英文常见列名）或纯数据两列；容忍 UTF-8 BOM。
校验：事件号连续递增、计数在 [0, 65535]、相邻间隔在 1..90000ms 内可行。
"""

from __future__ import annotations

import csv
import io

from .reconcile import MOD

EVENT_COL_NAMES = {"事件号", "事件", "event", "event_no", "eventno", "no", "id", "序号"}
COUNT_COL_NAMES = {"计数", "count", "counter", "value", "cnt"}


class CsvError(ValueError):
    pass


def _parse_int(raw: str, lineno: int, col: str) -> int:
    raw = raw.strip()
    if raw == "":
        raise CsvError(f"第 {lineno} 行 {col} 为空")
    try:
        value = int(raw)
    except ValueError as exc:
        raise CsvError(f"第 {lineno} 行 {col} 不是整数: {raw!r}") from exc
    return value


def parse_events_csv(text: str) -> list[dict]:
    """返回 [{"event_no", "count"}]，严格按 CSV 行顺序。"""
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        raise CsvError("CSV 为空")

    first = [c.strip().lower() for c in rows[0]]
    has_header = len(first) >= 2 and (
        first[0] in EVENT_COL_NAMES or first[1] in COUNT_COL_NAMES
    )
    data_rows = rows[1:] if has_header else rows
    if len(data_rows) < 2:
        raise CsvError("至少需要 2 个事件才能形成间隔")

    events: list[dict] = []
    prev_count: int | None = None
    for offset, row in enumerate(data_rows, start=2 if has_header else 1):
        if len(row) < 2:
            raise CsvError(f"第 {offset} 行不足两列")
        event_no = _parse_int(row[0], offset, "事件号")
        count = _parse_int(row[1], offset, "计数")
        if not 0 <= count <= MOD - 1:
            raise CsvError(f"第 {offset} 行计数 {count} 超出 0..{MOD - 1}")
        events.append({"event_no": event_no, "count": count})

    # 事件号校验：CSV 按事件顺序排列，要求事件号连续递增
    expected = events[0]["event_no"]
    for ev in events:
        if ev["event_no"] != expected:
            raise CsvError(
                f"事件号必须连续递增：期望 {expected}，实际 {ev['event_no']}"
            )
        expected += 1

    # 间隔可行性：模余数 r 恒在 [0,65535]，r=0 时间隔为 65536，
    # 否则间隔为 r（或 r+65536），最小候选恒 <= 65536 < 90000，
    # 因此任意相邻计数对都至少存在一个 1..90000ms 内的合法间隔，无需额外拒绝。
    return events
