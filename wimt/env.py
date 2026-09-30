"""프로젝트 폴더의 .env 를 읽어 환경 변수로 넣는다 (이미 설정된 값은 덮어쓰지 않는다)."""
import os
from pathlib import Path


def load_env(path: Path) -> list[str]:
    loaded = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8-sig").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
