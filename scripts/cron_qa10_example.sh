#!/usr/bin/env bash
# 예시: 배포 서버에서 주간 채팅 품질 점검 (crontab)
#
#   chmod +x /app/rag/fastapi/scripts/cron_qa10_example.sh
#   crontab -e
#   # 매주 월요일 09:10 (서버 로컬 시간)
#   10 9 * * 1 cd /app/rag/fastapi && ./scripts/cron_qa10_example.sh >> /var/log/rag_qa10.log 2>&1
#
# 환경: APP_ROOT, TEST_API_BASE_URL(또는 QA10_BASE_URL), 선택적 QA10_DELAY_SEC

set -euo pipefail
ROOT="${APP_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"

BASE="${TEST_API_BASE_URL:-${QA10_BASE_URL:-http://127.0.0.1:8000}}"
DELAY="${QA10_DELAY_SEC:-2}"

python scripts/qa_10_eval.py \
  --base-url "$BASE" \
  --delay "$DELAY" \
  --json-out "$ROOT/artifacts/qa10_chat_report.json" \
  --md-out "$ROOT/artifacts/qa10_operational_report.md"

python scripts/aggregate_quality_metrics.py --qa10-report "$ROOT/artifacts/qa10_chat_report.json"

echo "$(date -Iseconds) QA10 OK pass=$(python -c "import json;print(json.load(open('$ROOT/artifacts/qa10_chat_report.json'))['pass'])")" 
