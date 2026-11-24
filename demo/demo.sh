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
