#!/bin/bash

# 1. 사용자 설정
GITHUB_USER="zosungin"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"  # export GITHUB_TOKEN=... 로 주입
REPO_NAME="rag_v3"
# 현재 경로(/app/rag/fastapi)를 작업 디렉토리로 설정
TARGET_DIR="."

# 인증 정보가 포함된 URL 구성
if [ -z "$GITHUB_TOKEN" ]; then
    echo "오류: GITHUB_TOKEN 환경변수가 비어 있습니다."
    exit 1
fi
AUTH_URL="https://$GITHUB_USER:$GITHUB_TOKEN@github.com/$GITHUB_USER/$REPO_NAME.git"

echo "=== GitHub Private 소스 강제 동기화 시작 ==="

# 2. 잘못 생성된 하위 폴더가 있다면 제거 (중복 방지)
if [ -d "./$REPO_NAME" ]; then
    echo "하위 폴더 ./$REPO_NAME 발견. 경로 정리를 위해 삭제합니다."
    rm -rf "./$REPO_NAME"
fi

# 3. Git 동기화 로직
if [ -d ".git" ]; then
    echo "기존 Git 저장소 정보를 갱신합니다..."
    # 인증 주소 업데이트
    git remote set-url origin "$AUTH_URL"
    # 모든 원격 변경사항 가져오기
    git fetch --all
    # 로컬의 모든 차이점을 무시하고 GitHub의 main 브랜치 내용으로 현재 폴더(.)를 덮어씀
    git reset --hard origin/main
    # 새로 생긴 불필요한 파일 정리
    git clean -fd
else
    echo "현재 폴더에 Git 설정이 없습니다. 새로 복제합니다..."
    # 폴더를 만들지 않고 현재 위치(.)에 바로 복제
    git clone "$AUTH_URL" .
fi

echo "---------------------------------------"
echo "현재 적용된 최신 커밋 정보:"
git log -1 --oneline
echo "---------------------------------------"
echo "=== 동기화 완료 ==="
