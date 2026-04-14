from pathlib import Path

root = Path(__file__).resolve().parents[1]
text = (root / "app/services/agent.py").read_text(encoding="utf-8").replace("\r\n", "\n")
key = 'AGENT_SYSTEM_PROMPT = """'
i = text.index(key) + len(key)
close_line = (
    'found=false → details=[], summary="조회 결과 없음" '
    '/ 도구 미사용 임의 데이터 생성 절대 금지"""'
)
cend = text.rfind(close_line)
if cend < 0:
    raise SystemExit("closing line of AGENT_SYSTEM_PROMPT not found")
j = cend + len(close_line) - 3  # drop trailing """
body = text[i:j]
out = root / "app/prompts/agent_system.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(body, encoding="utf-8")
print("wrote", out, len(body), "chars")
