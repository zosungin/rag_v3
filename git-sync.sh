#!/bin/bash

# 1. 사용자 설정
GITHUB_USER="zosungin"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"  # export GITHUB_TOKEN=... 로 주입
REPO_NAME="rag_v3"

# 2. 인증 정보 확인 및 URL 구성
if [ -z "$GITHUB_TOKEN" ]; then
    echo "오류: GITHUB_TOKEN 환경변수가 비어 있습니다."
    exit 1
fi
AUTH_URL="https://$GITHUB_USER:$GITHUB_TOKEN@github.com/$GITHUB_USER/$REPO_NAME.git"

echo "=== GitHub 소스 업로드 시작 ==="

# 3. 원격 주소 강제 업데이트 (기존 인증 정보 갱신)
git remote set-url origin "$AUTH_URL"

# 4. 변경 사항 반영 및 커밋
git add .
COMMIT_MSG="Auto-sync: $(date '+%Y-%m-%d %H:%M:%S')"

# 커밋 실행 (변경된 내용이 있을 때만 실행)
if git commit -m "$COMMIT_MSG"; then
    # 5. GitHub로 전송
    if git push origin main; then
        echo "-------------------------------"
        echo "✅ $COMMIT_MSG"
        echo "✅ GitHub 업로드가 성공적으로 완료되었습니다!"
        echo "-------------------------------"
    else
        echo "❌ 에러: GitHub 전송(push)에 실패했습니다. 토큰 권한을 확인하세요."
    fi
else
    echo "ℹ️ 알림: 변경된 파일이 없어 커밋을 건너뜁니다."
fi
