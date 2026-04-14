#!/bin/bash

# 설정: 파일 경로 및 로그 경로
APP_DIR="/app/sync"
LOG_DIR="/app/sync/scheduler"
mkdir -p $LOG_DIR

echo "==========================================="
echo "인벤토리 동기화 테스트 시작: $(date)"
echo "==========================================="

# 1. Embedding 스크립트 목록 (우선순위 반영)
EMB_FILES=(
    "gen_embeddings_server_devices.py"
    "gen_embeddings_network_devices.py"
    "gen_embeddings_alert_events.py"
    "gen_embeddings_network_interfaces.py"
    "gen_embeddings_server_connect.py"
    "gen_embeddings_server_cpu.py"
    "gen_embeddings_server_errpt.py"
    "gen_embeddings_server_filesystems.py"
    "gen_embeddings_server_interfaces.py"
    "gen_embeddings_server_listen.py"
    "gen_embeddings_server_logs.py"
    "gen_embeddings_server_memory.py"
    "gen_embeddings_server_processes.py"
    "gen_embeddings_server_swap.py"
)

# 2. Chunking 스크립트 목록
CHUNK_FILES=(
    "gen_chunking_alerts.py"
    "gen_chunking_network_performance.py"
    "gen_chunking_server_connect.py"
    "gen_chunking_server_errpt.py"
    "gen_chunking_server_listen.py"
    "gen_chunking_server_logs.py"
    "gen_chunking_server_performance.py"
)

# 실행 함수 정의
run_script() {
    local FILE=$1
    if [ -f "$APP_DIR/$FILE" ]; then
        echo -n "[$(date +'%H:%M:%S')] Running $FILE... "
        python3 "$APP_DIR/$FILE" >> "$LOG_DIR/${FILE%.py}.log" 2>&1
        
        if [ $? -eq 0 ]; then
            echo "SUCCESS"
        else
            echo "FAILED (Check log)"
        fi
        sleep 2 # 테스트용 대기 시간 (필요시 조정)
    else
        echo "[ERROR] $FILE not found."
    fi
}

echo "--- 1단계: Embedding Scripts 시작 ---"
for script in "${EMB_FILES[@]}"; do
    run_script "$script"
done

echo ""
echo "--- 2단계: Chunking Scripts 시작 ---"
for script in "${CHUNK_FILES[@]}"; do
    run_script "$script"
done

echo "==========================================="
echo "모든 테스트 완료: $(date)"
echo "==========================================="
