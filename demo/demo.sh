#!/usr/bin/env bash
# 端到端演示脚本：对本地运行的网页（默认 127.0.0.1:8000）走完整核查流程。
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "$0")" && pwd)"

post_anchors() {
  local file="$1"
  while IFS=, read -r ev utc; do
    [ "$ev" = "事件号" ] && continue
    curl -s -X POST "$BASE/api/anchors" -H 'Content-Type: application/json' \
      -d "{\"event_no\":$ev,\"utc_ms\":$utc}" >/dev/null
  done < "$file"
}

echo "== 上传事件 CSV =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" | python3 -m json.tool | head -8

echo; echo "== 正确锚点账：期望 consistent =="
post_anchors "$DIR/anchors_good.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

echo; echo "== 重建并登记单错账：期望 inconsistent + 候选首值/末值/步长 =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_bad_single.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

echo; echo "== 双错账：期望“单处更正无法对平” =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
post_anchors "$DIR/anchors_bad_double.csv"
curl -s -X POST "$BASE/api/verify" | python3 -m json.tool

echo; echo "== 漏导核算：重建并登记两条端锚点（右端额外 +4 圈，期望最少漏导 1 条） =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
MOD = 65536
UTC = [1700000000000]
for gap in (1000, 4464, 20000, 70000, 50000):
    UTC.append(UTC[-1] + gap)

def post(path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))

post("/api/anchors", {"event_no": 1, "utc_ms": UTC[0]})
post("/api/anchors", {"event_no": 6, "utc_ms": UTC[5] + 4 * MOD})
result = post("/api/missing/compute", {"left_event": 1, "right_event": 6})["result"]
print("核算结论:", result["status"], "最少漏导:", result["total_missing"])
for sub in result["subsegments"]:
    for it in sub["intervals"]:
        print(
            f"  间隔 {it['left_event']}->{it['right_event']} "
            f"耗时 {it['elapsed_ms']}ms 漏导 {it['missing']} 条"
        )
saved = post("/api/missing/records", {"result": result})
print("已登记编号:", saved["record"]["id"], "时效:", saved["record"]["freshness"]["freshness"])
rid = saved["record"]["id"]
print("回查:", json.dumps(
    json.load(urllib.request.urlopen(urllib.request.Request(
        base + f"/api/missing/records/{rid}")))["record"]["freshness"],
    ensure_ascii=False))
PY

echo; echo "== 对不上留痕：端锚点时差小于计数最小耗时 =="
curl -s -F "file=@$DIR/events.csv" "$BASE/api/dataset" >/dev/null
curl -s -X POST "$BASE/api/anchors" -H 'Content-Type: application/json' \
  -d '{"event_no":1,"utc_ms":1700000000000}' >/dev/null
curl -s -X POST "$BASE/api/anchors" -H 'Content-Type: application/json' \
  -d '{"event_no":6,"utc_ms":1700000000001}' >/dev/null
curl -s -X POST "$BASE/api/missing/compute" -H 'Content-Type: application/json' \
  -d '{"left_event":1,"right_event":6}' | python3 -m json.tool
