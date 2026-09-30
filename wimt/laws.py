"""약관이 인용한 법령 조문: 인용 추출 → legalize-kr 에서 조회 → 캐시.

legalize-kr(https://legalize.kr)는 국가법령정보센터의 법령을 법령마다 Markdown 파일, 개정마다 git 커밋으로 옮긴 저장소다
(kr/{법령명 띄어쓰기 제거}/법률.md · 시행령.md · 시행규칙.md). 조회는 legalize-cli 의 문서화된 JSON 출력
(`legalize laws article <법령> <조> --date D --semantic 시행일자 --json`)을 쓴다.

- 약관 버전 날짜에 시행 중이던 조문을 가져온다 (변경 기록은 그 버전 날짜, 현재 조항은 오늘 기준).
- 조회 한 번에 GitHub API 를 10회 넘게 쓴다 (토큰 없으면 시간당 60회). 그래서 결과를 .cache/laws/articles.jsonl 에
  따로 캐시한다: 지난 날짜의 조문은 바뀌지 않으므로 영구, 현행 조문은 CURRENT_TTL_DAYS 동안. GITHUB_TOKEN 을 두면 5,000회.
- 법령 원문은 공공저작물이지만 약관 원문과 같이 로컬(.cache/)에만 둔다.
"""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "laws"
CURRENT = "현행"               # as_of 가 없을 때 (현재 조항이 인용한 법령)
CURRENT_TTL_DAYS = 30
TIMEOUT = 90                   # legalize-cli 호출 하나 (초)
ARTICLE_CHARS = 3000           # 근거로 넣을 조문 길이

# 약관에 자주 나오는 법령의 legalize-kr 폴더 이름 (띄어쓰기 제거, 가운뎃점은 'ㆍ'). 2026-09 raw 경로로 확인했다.
KNOWN = {
    "개인정보보호법", "전자상거래등에서의소비자보호에관한법률", "위치정보의보호및이용등에관한법률",
    "정보통신망이용촉진및정보보호등에관한법률", "게임산업진흥에관한법률", "약관의규제에관한법률", "청소년보호법",
    "콘텐츠산업진흥법", "장애인복지법", "장애인고용촉진및직업재활법", "전자금융거래법", "통신비밀보호법", "전기통신사업법",
    "민법", "민사소송법", "형법", "상법", "저작권법", "전자서명법", "소득세법", "국세기본법", "부가가치세법", "대외무역법",
    "금융소비자보호에관한법률", "표시ㆍ광고의공정화에관한법률", "할부거래에관한법률", "방문판매등에관한법률",
    "신용정보의이용및보호에관한법률", "영화및비디오물의진흥에관한법률", "음악산업진흥에관한법률", "전자문서및전자거래기본법",
    "여신전문금융업법", "법인세법", "문화산업진흥기본법", "재난및안전관리기본법", "가축전염병예방법",
    "감염병의예방및관리에관한법률", "실종아동등의보호및지원에관한법률", "자살예방및생명존중문화조성을위한법률",
    "정신건강증진및정신질환자복지서비스지원에관한법률", "국가경찰과자치경찰의조직및운영에관한법률",
}
# 약칭·옛 표기 -> 폴더 이름
ALIASES = {
    "정보통신망법": "정보통신망이용촉진및정보보호등에관한법률",
    "정보통신망이용촉진및정보보호에관한법률": "정보통신망이용촉진및정보보호등에관한법률",
    "위치정보법": "위치정보의보호및이용등에관한법률",
    "전자상거래법": "전자상거래등에서의소비자보호에관한법률",
    "전자상거래소비자보호법": "전자상거래등에서의소비자보호에관한법률",
    "게임산업법": "게임산업진흥에관한법률",
    "게임산업진흥법": "게임산업진흥에관한법률",
    "약관규제법": "약관의규제에관한법률",
    "금융소비자보호법": "금융소비자보호에관한법률",
    "표시광고법": "표시ㆍ광고의공정화에관한법률",
    "할부거래법": "할부거래에관한법률",
    "방문판매법": "방문판매등에관한법률",
    "신용정보법": "신용정보의이용및보호에관한법률",
    "직업재활법": "장애인고용촉진및직업재활법",
    "재난안전법": "재난및안전관리기본법",
    "감염병예방법": "감염병의예방및관리에관한법률",
    "실종아동법": "실종아동등의보호및지원에관한법률",
    "자살예방법": "자살예방및생명존중문화조성을위한법률",
    "정신건강복지법": "정신건강증진및정신질환자복지서비스지원에관한법률",
    # 전부개정으로 이름이 바뀐 법(정신보건법, 경찰법)은 조 번호가 달라 옛 이름을 새 폴더로 옮기지 않는다 (조회에서 없음)
}
SAME_LAW = {"동법", "같은법", "동", "같은"}      # "동법 제3조", "같은 법 시행령 제5조" -> 바로 앞에 나온 법령
NOT_A_NAME = {"법", "법률", "이법", "본법", "관계법", "관련법", "관계법률", "관련법률", "해당법", "해당법률", "특별법", "개별법"}

# (법령 이름 끝) (시행령|시행규칙)? 제N조(의M)?
REF = re.compile(r"(법률|법)\s*(시행령|시행규칙)?\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
# 법령 본문 안의 같은 법 인용 ("제15조제1항제2호")
BARE = re.compile(r"(?<![가-힣])제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
QUOTES = str.maketrans({c: " " for c in "「」『』‘’“”\"'<>《》〈〉()（）[]"})


@dataclass(frozen=True)
class Ref:
    law: str          # legalize-kr 폴더 이름
    category: str     # 법률 | 시행령 | 시행규칙
    article: str      # 제17조, 제17조의2
    as_of: str        # YYYY-MM-DD 또는 CURRENT

    @property
    def key(self) -> str:
        return f"{self.law}/{self.category}::{self.article}@{self.as_of}"


def law_name(tokens: list[str], names: set[str] | None = None) -> str | None:
    """'…에 따라 개인정보 보호' + '법' 처럼 조 번호 앞의 낱말들 -> 폴더 이름. 긴 이름부터 맞춰 본다."""
    names = (names or set()) | KNOWN
    for i in range(max(0, len(tokens) - 12), len(tokens)):
        cand = "".join(tokens[i:]).replace("·", "ㆍ").replace("・", "ㆍ")
        if cand in names:
            return cand
        if cand in ALIASES:
            return ALIASES[cand]
    last = tokens[-1] if tokens else ""
    # 목록에 없는 한 낱말 법령 ("의료기기법"). 조사가 붙은 말은 빼고, 없는 이름이면 조회에서 걸러진다 (not found 캐시)
    if len(last) >= 3 and last not in NOT_A_NAME and re.fullmatch(r"[가-힣ㆍ]+(법|법률)", last) \
            and not re.search(r"(에|의|및|과|와|을|를|은|는|이|가|로|에서|따른|의한|관한|위한)(법|법률)$", last):
        return last
    return None


def extract(text: str, as_of: str = CURRENT, names: set[str] | None = None,
            same_law: tuple[str, str] | None = None) -> list[Ref]:
    """본문에서 법령 조문 인용을 순서대로 (중복 없이).

    same_law=(법령, 구분)을 주면 법령 본문으로 보고 법령 이름 없는 "제N조"도 그 법령의 조문으로 본다."""
    text = text.translate(QUOTES)
    out, last = [], None
    for m in REF.finditer(text):
        head = text[max(0, m.start() - 80):m.start()] + m.group(1)
        tokens = head.split()
        if tokens and tokens[-1] in ("법", "법률") and len(tokens) >= 2 and tokens[-2] in SAME_LAW:
            name = last
        elif tokens and tokens[-1] in ("동법", "같은법"):
            name = last
        else:
            name = law_name(tokens, names)
        if not name:
            continue
        last = name
        article = f"제{m.group(3)}조" + (f"의{m.group(4)}" if m.group(4) else "")
        out.append(Ref(name, m.group(2) or "법률", article, as_of))
    if same_law:
        law, category = same_law
        named = {(m.start(), m.end()) for m in REF.finditer(text)}
        for m in BARE.finditer(text):
            if any(s <= m.start() < e for s, e in named):
                continue
            out.append(Ref(law, category, f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else ""), as_of))
    return list(dict.fromkeys(out))


class LawError(Exception):
    """조회 실패 (한도 초과·네트워크 등). 없는 조문(not found)은 오류가 아니라 None."""


class RateLimited(LawError):
    pass


class Client:
    """legalize-cli 로 조문 하나를 조회한다. 결과(없는 조문 포함)를 캐시해 같은 조회는 API 를 쓰지 않는다."""

    def __init__(self, cache_dir: Path = CACHE, runner=subprocess.run, today=date.today):
        self.cache_dir, self.runner, self.today = Path(cache_dir), runner, today
        self.path = self.cache_dir / "articles.jsonl"
        self.cache: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.cache[row["key"]] = row

    def cached(self, ref: Ref) -> dict | None:
        row = self.cache.get(ref.key)
        if row and ref.as_of == CURRENT and date.fromisoformat(row["fetched"]) < self.today() - timedelta(CURRENT_TTL_DAYS):
            return None
        return row

    def article(self, ref: Ref) -> dict | None:
        """조문 (없으면 None). 한도 초과면 RateLimited, 그 밖의 실패는 LawError."""
        row = self.cached(ref)
        if row is None:
            row = {"key": ref.key, "fetched": self.today().isoformat(), **self._fetch(ref)}
            self.cache[ref.key] = row
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row if row.get("found") else None

    def _fetch(self, ref: Ref) -> dict:
        cmd = [sys.executable, "-m", "legalize_cli", "laws", "article", ref.law, ref.article,
               "--category", ref.category, "--semantic", "시행일자", "--json",
               "--cache-dir", str(self.cache_dir / "legalize-cli")]
        if ref.as_of != CURRENT:
            cmd += ["--date", ref.as_of]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            p = self.runner(cmd, capture_output=True, timeout=TIMEOUT, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LawError(f"{type(e).__name__}: {e}") from e
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if p.returncode == 4:                      # NotFoundError: 없는 법령·조문, 그 날짜의 개정본 없음
            return {"found": False, "error": err[:300]}
        if p.returncode == 7:
            raise RateLimited(err[:300] or "GitHub API 한도 초과 (GITHUB_TOKEN 을 설정하세요)")
        if p.returncode != 0:
            raise LawError(f"legalize-cli 종료 코드 {p.returncode}: {err[:300]}")
        got = json.loads(p.stdout.decode("utf-8"))
        return {"found": True, "law": ref.law, "category": ref.category, "article": ref.article, "as_of": ref.as_of,
                "resolved": got.get("resolved_version_date") or "", "effective_date": got.get("시행일자") or "",
                "promulgated": got.get("공포일자") or "", "source_url": got.get("출처") or "",
                "status": got.get("status") or "", "heading": " > ".join(got.get("parent_structure") or []),
                "text": (got.get("content") or "")[:ARTICLE_CHARS]}


def display(law: str) -> str:
    """폴더 이름을 읽기 좋게 (띄어쓰기 없이 그대로 두되 가운뎃점만 되돌린다)."""
    return law.replace("ㆍ", "·")
