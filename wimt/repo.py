"""약관 저장소(TOS-Korea-Project) 읽기: 문서 목록, 버전 이력, 문서 메타데이터(데이터 카드).

저장소 규칙: 약관 파일 하나 = 문서, 그 파일을 바꾼 커밋 하나 = 버전.
"""
import datetime as dt
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[2] / "TOS-Korea-Project"

# 회사 폴더 -> 영역. 제안서의 영역 구분(게임, 카드, 마일리지·포인트, 쇼핑몰·OTT)에 통신을 더했다.
DOMAINS = {"coupang": "쇼핑몰", "tving": "OTT", "kt": "통신", "krafton": "게임", "nexon": "게임", "riotgames": "게임",
           "naver": "인터넷 서비스", "navercloud": "클라우드", "kakao": "인터넷 서비스"}

# 회사 폴더 -> 수집 방식 (TOS-Korea-Project 를 만들 때 실제로 쓴 방식)
FETCH_METHODS = {"coupang": "browser", "tving": "browser", "nexon": "browser",
                 "kt": "api", "krafton": "page-data", "riotgames": "api", "navercloud": "api"}


@dataclass
class Version:
    commit: str
    date: dt.date          # 커밋 날짜 = 버전일자 (수집 규칙상)
    meta: dict             # frontmatter
    body: str              # 본문 원문


def git(repo, *args) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return out.stdout.decode("utf-8")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """`---` 로 둘러싼 단순 `키: 값` frontmatter 와 본문을 나눈다."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    _, fm, body = text.split("---\n", 2)
    meta = {}
    for line in fm.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        value = value.split("  #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        meta[key.strip()] = None if value == "null" else value
    return meta, body.lstrip("\n")


def documents(repo=DEFAULT_REPO) -> list[str]:
    """저장소의 약관 파일 경로 목록 (README 제외)."""
    out = git(repo, "ls-files", "-z", "--", "*.md")
    # 루트와 회사 폴더의 README.md 는 약관이 아니라 설명 문서
    return sorted(p for p in out.split("\0")
                  if p and "/" in p and p.split("/", 1)[0] != "docs"
                  and not p.startswith(".") and p.rsplit("/", 1)[-1].lower() != "readme.md")


def history(path: str, repo=DEFAULT_REPO) -> list[Version]:
    """문서의 버전 목록, 오래된 것부터."""
    log = git(repo, "log", "--reverse", "--format=%H%x09%ad", "--date=short", "--", path)
    versions = []
    for line in log.splitlines():
        commit, date = line.split("\t")
        meta, body = split_frontmatter(git(repo, "show", f"{commit}:{path}"))
        versions.append(Version(commit, dt.date.fromisoformat(date), meta, body))
    return versions


def service_of(path: str) -> str:
    """`krafton/pubg/운영정책(Steam).md` -> `krafton/pubg`, `coupang/x.md` -> `coupang`."""
    parts = path.split("/")
    return "/".join(parts[:-1])


def doc_type(title: str) -> str:
    """제안서의 문서 종류 태그: 약관, 운영정책, 부가서비스, 개인정보처리방침, 대회 규정 (+ 동의서, 기타)."""
    t = title.replace(" ", "")
    if "동의" in t:
        return "동의서"
    if "개인정보" in t and ("방침" in t or "고지" in t):
        return "개인정보처리방침"
    if "재화" in t or "포인트" in t or "멤버십" in t or "유료" in t:
        return "부가서비스"
    if "운영" in t and any(k in t for k in ("정책", "원칙", "기준")):
        return "운영정책"
    if "규정" in t or "규약" in t or "규율" in t:
        return "대회 규정"
    if "약관" in t or "이용기준" in t:
        return "약관"
    return "기타"


def data_card(path: str, repo=DEFAULT_REPO, versions: list[Version] | None = None) -> dict:
    """제안서 2단계 데이터 카드: 서비스 · 영역 · 문서 종류 · 출처 URL · 수집 방식 · 최초 수집일 · 변경 횟수 · 공개 여부."""
    versions = versions if versions is not None else history(path, repo)
    latest = versions[-1].meta
    company = path.split("/")[0]
    return {
        "path": path,
        "service": service_of(path),
        "title": latest.get("제목"),
        "domain": DOMAINS.get(company, "기타"),
        "doc_type": doc_type(latest.get("제목") or Path(path).stem),
        "source_url": latest.get("출처"),
        "fetch_method": FETCH_METHODS.get(company, "unknown"),
        "first_fetched": min(v.meta.get("수집일자") or "" for v in versions) or None,
        "first_version": versions[0].date.isoformat(),
        "latest_version": versions[-1].date.isoformat(),
        "change_count": len(versions) - 1,
        "is_public": True,  # 공식 공개 문서만 수집
    }
