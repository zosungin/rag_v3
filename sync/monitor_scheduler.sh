#!/bin/bash

echo "========================================"
echo "📊 스케줄러 모니터링 대시보드"
echo "========================================"
echo "시간: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 1. 프로세스 상태
echo "1️⃣ 프로세스 상태:"
if ps aux | grep -v grep | grep scheduler.py > /dev/null; then
    echo "   ✅ 실행 중"
    ps aux | grep -v grep | grep scheduler.py | awk '{print "   PID: "$2", CPU: "$3"%, MEM: "$4"%"}'
else
    echo "   ❌ 중지됨"
fi

# 2. 최근 실행 기록
echo ""
echo "2️⃣ 최근 실행 기록 (최근 3회):"
grep "스케줄 작업 시작" /app/sync/scheduler.log | tail -3

# 3. 성공/실패 통계
echo ""
echo "3️⃣ 실행 통계:"
total_runs=$(grep -c "스케줄 작업 시작" /app/sync/scheduler.log)
success_runs=$(grep -c "스케줄 작업 완료" /app/sync/scheduler.log)
echo "   총 실행: ${total_runs}회"
echo "   성공: ${success_runs}회"
echo "   실패: $((total_runs - success_runs))회"

# 4. 처리 건수 추이
echo ""
echo "4️⃣ 최근 처리 건수 (최근 3회):"
grep "처리 대상" /app/sync/scheduler.log | tail -3 | awk '{print "   "$0}'

# 5. Vector DB 상태
echo ""
echo "5️⃣ Vector DB 레코드 수:"
record_count=$(docker exec -it rag-pgvector psql -U rag_user -d rag_db -t -c "SELECT COUNT(*) FROM embeddings_items;" 2>/dev/null | xargs)
echo "   총 레코드: ${record_count}건"

# 6. 최근 에러
echo ""
echo "6️⃣ 최근 에러 (있는 경우):"
recent_errors=$(grep "ERROR\|❌" /app/sync/scheduler.log | tail -3)
if [ -z "$recent_errors" ]; then
    echo "   ✅ 에러 없음"
else
    echo "$recent_errors" | awk '{print "   "$0}'
fi

# 7. 다음 실행 예정
echo ""
echo "7️⃣ 다음 실행 예정:"
last_run=$(grep "스케줄 작업 시작" /app/sync/scheduler.log | tail -1 | awk '{print $1" "$2}')
if [ ! -z "$last_run" ]; then
    next_run=$(date -d "$last_run 2 hours" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "계산 불가")
    echo "   마지막 실행: $last_run"
    echo "   다음 실행: $next_run (약)"
fi

echo ""
echo "========================================"

