"""生成演示用事件 CSV 与两套锚点（正确账 / 抄错账）。

计数规则：每毫秒 +1，满 65536 归零。
演示账的相邻间隔刻意包含“模糊间隔”（余数 <= 24464），
以验证核查不是简单差值比对，而是按可达等差数列核对。
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reconcile import MOD  # noqa: E402

# 事件 1..6 的真实间隔（毫秒），全部落在 1..90000 内
# 判定依据是“计数模差 r”而非间隔本身：r <= 24464 时间隔模糊（另有 r+65536 候选）。
# 1000 -> r=1000 模糊；4464 -> r=4464 模糊；20000 -> r=20000 模糊；
# 70000 -> r=4464 模糊；50000 -> r=50000 > 24464，固定间隔。
GAPS = [1000, 4464, 20000, 70000, 50000]
START_UTC = 1_700_000_000_000  # 任意基准 UTC 毫秒
START_COUNT = 12345


def build() -> tuple[list[dict], list[int]]:
    counts = [START_COUNT]
    utc = [START_UTC]
    count = START_COUNT
    t = START_UTC
    for gap in GAPS:
        count = (count + gap) % MOD
        t += gap
        counts.append(count)
        utc.append(t)
    events = [{"event_no": i + 1, "count": c} for i, c in enumerate(counts)]
    return events, utc


def write_csv(path: str, events: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["事件号", "计数"])
        for ev in events:
            writer.writerow([ev["event_no"], ev["count"]])


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    events, utc = build()
    write_csv(os.path.join(out_dir, "events.csv"), events)

    # 正确锚点账（事件号 -> 纸本 UTC 毫秒），至少 3 条
    good = [(1, utc[0]), (3, utc[2]), (5, utc[4])]
    with open(os.path.join(out_dir, "anchors_good.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["事件号", "纸本UTC毫秒"])
        w.writerows(good)

    # 抄错账：事件 3 被多抄三圈（超出区间模糊跨度，必现不一致），
    # 只有事件 3 本身可单改对平，候选时刻是步长 65536 的等差数列
    bad = [(1, utc[0]), (3, utc[2] + 3 * MOD), (5, utc[4])]
    with open(os.path.join(out_dir, "anchors_bad_single.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["事件号", "纸本UTC毫秒"])
        w.writerows(bad)

    # 双错账：四锚点中事件 1 少抄四圈、事件 2 多抄一圈，单处更正无法对平
    bad2 = [
        (1, utc[0] - 4 * MOD),
        (2, utc[1] + MOD),
        (4, utc[3]),
        (6, utc[5]),
    ]
    with open(os.path.join(out_dir, "anchors_bad_double.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["事件号", "纸本UTC毫秒"])
        w.writerows(bad2)

    print("真实 UTC 毫秒：", utc)
    print("事件计数：", [e["count"] for e in events])
    print("正确锚点：", good)
    print("单错锚点：", bad)
    print("双错锚点：", bad2)
    print(f"已写入 {out_dir}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__))
    )
    main(target)
