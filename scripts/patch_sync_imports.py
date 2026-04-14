"""
sync/*.py 상단의 하드코딩 DB/OLLAMA 설정을 app.config 기반으로 치환.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "sync"

BOOTSTRAP = '''import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, ZABBIX_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

'''

BOOTSTRAP_RAG_ONLY = '''import os, sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from app.config import RAG_DB, OLLAMA_EMBED_URL, EMBEDDING_MODEL
OLLAMA_API_URL = OLLAMA_EMBED_URL
OLLAMA_MODEL = EMBEDDING_MODEL

'''

# ZABBIX + RAG 한 줄 dict 스타일
PAT_ONE_LINE = re.compile(
    r"^ZABBIX_DB = \{[^\n]+\}\nRAG_DB = \{[^\n]+\}\nOLLAMA_API_URL = \"[^\n]+\"\nOLLAMA_MODEL = \"[^\n]+\"\n",
    re.MULTILINE,
)


def patch_file(path: Path, rag_only: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    if "from app.config import RAG_DB" in text:
        return False
    boot = BOOTSTRAP_RAG_ONLY if rag_only else BOOTSTRAP
    new = text
    if rag_only:
        new = re.sub(
            r"^RAG_DB = \{[^\n]+\}\nOLLAMA_API_URL = \"[^\n]+\"\nOLLAMA_MODEL = \"[^\n]+\"\n",
            "",
            new,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        m = PAT_ONE_LINE.search(new)
        if m:
            new = new[: m.start()] + new[m.end() :]
        else:
            # multiline ZABBIX (errpt)
            new2 = re.sub(
                r"^ZABBIX_DB = \{[^}]+\}\s*\n\s*'password':[^\n]+\n\}\nRAG_DB = \{[^}]+\}\s*\n\s*'password':[^\n]+\n\}\nOLLAMA_API_URL = \"[^\n]+\"\nOLLAMA_MODEL = \"[^\n]+\"\n",
                "",
                new,
                count=1,
                flags=re.MULTILINE | re.DOTALL,
            )
            if new2 == new:
                print("SKIP (pattern mismatch):", path.name)
                return False
            new = new2
    # 첫 import 전에 bootstrap 삽입
    lines = new.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    # skip blank after shebang
    while insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    out = "".join(lines[:insert_at]) + boot + "".join(lines[insert_at:])
    path.write_text(out, encoding="utf-8")
    return True


def main():
    zabbix_files = [
        "gen_embeddings_server_logs.py",
        "gen_embeddings_server_memory.py",
        "gen_embeddings_server_processes.py",
        "gen_embeddings_server_swap.py",
        "gen_embeddings_server_listen.py",
        "gen_embeddings_server_interfaces.py",
        "gen_embeddings_alert_events.py",
        "gen_embeddings_network_interfaces.py",
        "gen_embeddings_network_devices.py",
        "gen_embeddings_server_connect.py",
        "gen_embeddings_server_cpu.py",
        "gen_embeddings_server_devices.py",
        "gen_embeddings_server_filesystems.py",
    ]
    for name in zabbix_files:
        p = SYNC / name
        if p.is_file():
            patch_file(p, rag_only=False)
            print("patched", name)
    errpt = SYNC / "gen_embeddings_server_errpt.py"
    if errpt.is_file():
        t = errpt.read_text(encoding="utf-8")
        if "from app.config import RAG_DB" not in t:
            t = re.sub(
                r"ZABBIX_DB = \{[\s\S]*?\}\n\nRAG_DB = \{[\s\S]*?\}\n\nOLLAMA_API_URL = \"[^\"]+\"\nOLLAMA_MODEL = \"[^\"]+\"\n",
                "",
                t,
                count=1,
            )
            lines = t.splitlines(keepends=True)
            ins = 0
            if lines and lines[0].startswith("#!"):
                ins = 1
            while ins < len(lines) and lines[ins].strip() == "":
                ins += 1
            errpt.write_text(
                "".join(lines[:ins]) + BOOTSTRAP + "".join(lines[ins:]),
                encoding="utf-8",
            )
            print("patched gen_embeddings_server_errpt.py")
    rag_only = [
        "gen_chunking_alerts.py",
        "gen_chunking_network_performance.py",
        "gen_chunking_server_connect.py",
        "gen_chunking_server_errpt.py",
        "gen_chunking_server_inventory.py",
        "gen_chunking_server_listen.py",
        "gen_chunking_server_logs.py",
        "gen_chunking_server_performance.py",
        "gen_chunking_server_process_topn.py",
    ]
    for name in rag_only:
        p = SYNC / name
        if p.is_file():
            if patch_file(p, rag_only=True):
                print("patched", name)


if __name__ == "__main__":
    main()
