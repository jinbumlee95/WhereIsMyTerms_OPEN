"""약관이 인용한 법령 조문: 인용 추출 → legalize-kr 에서 조회 → 캐시.

legalize-kr(https://legalize.kr)는 국가법령정보센터의 법령을 법령마다 Markdown 파일, 개정마다 git 커밋으로 옮긴 저장소다
(kr/{법령명 띄어쓰기 제거}/법률.md · 시행령.md · 시행규칙.md). 커밋 날짜는 공포일자, 파일 머리말에 시행일자가 있다.

- 약관 버전 날짜에 시행 중이던 판의 조문을 가져온다 (변경 기록은 그 버전 날짜, 현재 조항은 오늘 기준).
- 법령 파일 단위로 받는다: 커밋 목록(GitHub API 1회) → 기준일에 시행 중인 판 → 그 판 파일(raw, API 한도 안 씀) 한 번.
  같은 판이면 조문 여러 개·날짜 여러 개가 파일 하나를 함께 쓴다 (2026-09-30 이전: 조문마다 legalize-cli 를 실행해
  조문 하나에 6~9초, 제목 목록 하나에 7~30초가 걸렸다). GITHUB_TOKEN 을 두면 API 한도가 시간당 5,000회.
- 법령 원문은 공공저작물이지만 약관 원문과 같이 로컬(.cache/)에만 둔다.
"""
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "laws"
CURRENT = "현행"               # as_of 가 없을 때 (현재 조항이 인용한 법령)
REPO = "legalize-kr/legalize-kr"
COMMITS_TTL_DAYS = 1          # 법령 커밋 목록을 다시 받는 주기 (새 개정 반영)
TIMEOUT = 30                   # GitHub 요청 하나 (초)
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
        if tokens and tokens[-1] in ("법", "법률") and len(tokens) >= 2 and tokens[-2] in SAME_LAW or tokens and tokens[-1] in ("동법", "같은법"):
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


# ---------------------------------------------------------------------------
# 조 번호 없이 법령 이름만 인용한 경우 ("'전자상거래 등에서의 소비자보호에 관한 법률'에 따른 사항입니다")
# 그 법령 안에서만, 인용 문맥(조항 제목 + 앞뒤 문장)의 낱말과 조문 제목이 맞는 조문을 고른다. 다른 법령으로 넓히지 않는다.
# ---------------------------------------------------------------------------
NAME_WINDOW = (200, 100)          # 법령 이름 앞뒤로 문맥에 넣을 글자 수
NAME_ONLY_MAX = 2                 # 이름만 인용한 법령 하나에서 가져올 조문 수
TITLE_MIN_SCORE = 3               # 맞은 낱말 길이 합이 이보다 작으면 고르지 않는다 ("제공"처럼 흔한 두 글자 하나로는 부족)
GENERIC_TITLE_TERMS = {"목적", "정의", "적용", "범위", "적용범위", "적용제외", "관계", "벌칙", "과태료", "시행", "보칙", "총칙",
                       "효과", "특례", "위임", "권한", "다른", "법률", "등", "절차", "기준", "방법", "사항", "경우"}


def _name_pattern(name: str) -> re.Pattern:
    """띄어쓰기를 지운 법령 이름 -> 본문에서 띄어쓰기·가운뎃점 변형을 허용하는 정규식."""
    chars = [r"[ㆍ·・]" if c == "ㆍ" else re.escape(c) for c in name]
    return re.compile(r"\s*".join(chars))


_NAME_PATTERNS = None


def _patterns() -> list[tuple[str, re.Pattern]]:
    global _NAME_PATTERNS
    if _NAME_PATTERNS is None:        # 긴 이름부터 (약칭이 긴 이름 속에 들어 있어도 긴 쪽이 먼저)
        pairs = [(n, n) for n in KNOWN] + list(ALIASES.items())
        _NAME_PATTERNS = [(law, _name_pattern(alias)) for alias, law in sorted(pairs, key=lambda p: -len(p[0]))]
    return _NAME_PATTERNS


def extract_names(text: str, as_of: str = CURRENT) -> list[tuple[Ref, str]]:
    """조 번호 없이 이름만 인용한 법령과 그 문맥: [(Ref(article=""), 문맥)]. 법령마다 한 번.
    뒤에 "제N조"가 붙은 인용은 extract 가 맡으므로 뺀다."""
    text = text.translate(QUOTES)
    taken: list[tuple[int, int]] = []
    found: dict[str, tuple[Ref, str]] = {}
    for law, pat in _patterns():
        for m in pat.finditer(text):
            if any(s <= m.start() < e for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            after = text[m.end():m.end() + 20]
            if re.match(r"\s*(시행령|시행규칙)?\s*제\s*\d+\s*조", after):
                continue
            category = (re.match(r"\s*(시행령|시행규칙)", after) or [None, "법률"])[1]
            # 문맥에서 법령 이름 자체는 뺀다 (이름 속 낱말이 조문 제목과 맞아 버리지 않게: "전자상거래등에서의…")
            window = text[max(0, m.start() - NAME_WINDOW[0]):m.start()] + " " + text[m.end():m.end() + NAME_WINDOW[1]]
            key = f"{law}/{category}"
            if key not in found:
                found[key] = (Ref(law, category, "", as_of), window)
    return list(found.values())


def title_terms(title: str) -> list[str]:
    """조문 제목 -> 맞춰 볼 낱말 ("청약철회등의 효과" -> ["청약철회"]). 조사·'등'을 떼고 흔한 말은 뺀다."""
    out = []
    for w in re.split(r"[\sㆍ·,]+", title):
        for suffix in ("에서의", "에관한", "에대한", "에따른", "등의"):
            if w.endswith(suffix) and len(w) > len(suffix):
                w = w[:-len(suffix)]
        if w.endswith("의") and len(w) >= 4:
            w = w[:-1]
        if w.endswith("등") and len(w) >= 3:
            w = w[:-1]
        if len(w) >= 2 and w not in GENERIC_TITLE_TERMS:
            out.append(w)
    return out


def pick_articles(headings: list[tuple[str, str]], context: str, limit: int = NAME_ONLY_MAX) -> list[str]:
    """조문 제목 [(제17조, 청약철회등)] 중 문맥에 낱말이 들어 있는 것을 limit 개.
    순서: 맞은 낱말 길이 합 → 제목 낱말 중 맞은 비율 (제목이 통째로 맞는 조문이 먼저) → 조문 순서."""
    ctx = re.sub(r"\s+", "", context)
    scored = []
    for i, (article, title) in enumerate(headings):
        terms = title_terms(title)
        hit = [t for t in terms if t in ctx]
        score = sum(len(t) for t in hit)
        if score >= TITLE_MIN_SCORE:
            scored.append((-score, -len(hit) / len(terms), i, article))
    return [s[-1] for s in sorted(scored)[:limit]]


class LawError(Exception):
    """조회 실패 (한도 초과·네트워크 등). 없는 조문(not found)은 오류가 아니라 None."""


class RateLimited(LawError):
    pass


class GitHub:
    """legalize-kr 저장소 읽기: 파일의 커밋 목록(API)과 특정 커밋의 파일(raw, API 한도를 쓰지 않는다)."""
    API = "https://api.github.com/repos/" + REPO
    RAW = "https://raw.githubusercontent.com/" + REPO

    def __init__(self, token: str | None = None):

        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
        self.http = httpx.Client(timeout=TIMEOUT, headers=headers, follow_redirects=True)

    def commits(self, path: str) -> list[tuple[str, str]]:
        """[(sha, 커밋 날짜 YYYY-MM-DD)] 최신순. 커밋 날짜는 공포일자다 (legalize-kr 규칙). 없는 파일이면 []."""
        out, page = [], 1
        while True:
            r = self._get(f"{self.API}/commits", params={"path": path, "per_page": 100, "page": page})
            got = r.json()
            out += [(c["sha"], c["commit"]["author"]["date"][:10]) for c in got]
            if len(got) < 100:
                return out
            page += 1

    def raw(self, sha: str, path: str) -> str | None:
        r = self._get(f"{self.RAW}/{sha}/{quote(path)}", allow_404=True)
        return None if r is None else r.text

    def _get(self, url, params=None, allow_404=False):

        try:
            r = self.http.get(url, params=params)
        except httpx.HTTPError as e:
            raise LawError(f"{type(e).__name__}: {e}") from e
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited("GitHub API 한도 초과 (GITHUB_TOKEN 을 설정하세요)")
        if r.status_code >= 400:
            raise LawError(f"GitHub {r.status_code}: {url}")
        return r


class Client:
    """조문 조회기. 법령 파일 단위로 받아 캐시하고, 조문·조문 제목은 그 파일에서 꺼낸다.

    - 판 고르기: 법령 파일의 커밋 목록(공포일자 순)에서 기준일 이전에 공포된 판을 최신부터 보며,
      머리말의 시행일자가 기준일 이전인 첫 판을 쓴다 (공포됐지만 아직 시행 전인 판은 건너뛴다).
    - 캐시 (.cache/laws/): 커밋 목록은 COMMITS_TTL_DAYS 동안, 판 파일은 커밋 sha 로 영구 (내용이 바뀌지 않는다).
      같은 법령·같은 판이면 조문이 여러 개여도, 날짜가 달라도 파일은 한 번만 받는다.
    - 여러 스레드가 함께 쓴다: 법령 파일(경로)마다 잠금을 따로 둬서 서로 다른 법령은 동시에 받는다.
    """

    def __init__(self, cache_dir: Path = CACHE, source=None, today=date.today):
        self.cache_dir, self.today = Path(cache_dir), today
        self.source = source or GitHub()
        self.files_dir = self.cache_dir / "files"
        self.commits_path = self.cache_dir / "commits.json"
        self.lock = threading.Lock()                      # 공유 dict·캐시 파일 쓰기
        self.path_locks: dict[str, threading.Lock] = {}   # 법령 파일마다 (같은 파일을 두 번 받지 않게)
        self.parsed: dict[str, dict] = {}                 # sha:path -> {meta, articles, headings}
        try:
            self.commit_cache = json.loads(self.commits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.commit_cache = {}

    # ---- 공개 ------------------------------------------------------------------
    def article(self, ref: Ref) -> dict | None:
        """조문 (없으면 None). 한도 초과면 RateLimited, 그 밖의 실패는 LawError."""
        doc = self._version(ref)
        if doc is None:
            return None
        body = doc["articles"].get(ref.article)
        if body is None:
            return None
        meta = doc["meta"]
        return {"found": True, "law": ref.law, "category": ref.category, "article": ref.article, "as_of": ref.as_of,
                "resolved": meta.get("시행일자", ""), "effective_date": meta.get("시행일자", ""),
                "promulgated": meta.get("공포일자", ""), "source_url": meta.get("출처", ""),
                "status": meta.get("상태", ""), "heading": body["parent"], "text": body["text"][:ARTICLE_CHARS]}

    def headings(self, ref: Ref) -> list[tuple[str, str]]:
        """그 날짜에 시행 중이던 판의 조문 제목 [(제17조, 청약철회등)]. 법령이 없으면 빈 목록."""
        doc = self._version(ref)
        return doc["headings"] if doc else []

    def resolve(self, ref: Ref, context: str) -> list[Ref]:
        """이름만 인용한 법령 ref(article="") -> 문맥과 조문 제목이 맞는 조문들 (최대 NAME_ONLY_MAX)."""
        return [Ref(ref.law, ref.category, a, ref.as_of) for a in pick_articles(self.headings(ref), context)]

    # ---- 판 고르기·캐시 -----------------------------------------------------------
    def _path_lock(self, path: str) -> threading.Lock:
        with self.lock:
            return self.path_locks.setdefault(path, threading.Lock())

    def _version(self, ref: Ref) -> dict | None:
        path = f"kr/{ref.law}/{ref.category}.md"
        day = self.today().isoformat() if ref.as_of == CURRENT else ref.as_of
        with self._path_lock(path):
            for sha, committed in self._commits(path):
                if committed > day:                       # 기준일 뒤에 공포된 판
                    continue
                doc = self._file(sha, path)
                if doc is None:
                    continue
                effective = doc["meta"].get("시행일자") or committed
                if effective <= day:                      # 공포됐지만 아직 시행 전인 판은 건너뛴다
                    return doc
        return None

    def _commits(self, path: str) -> list[tuple[str, str]]:
        row = self.commit_cache.get(path)
        if row and date.fromisoformat(row["fetched"]) >= self.today() - timedelta(COMMITS_TTL_DAYS):
            return [tuple(c) for c in row["commits"]]
        commits = self.source.commits(path)
        with self.lock:
            self.commit_cache[path] = {"fetched": self.today().isoformat(), "commits": commits}
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.commits_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.commit_cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.commits_path)
        return commits

    def _file(self, sha: str, path: str) -> dict | None:
        key = f"{sha}:{path}"
        if key in self.parsed:
            return self.parsed[key]
        cached = self.files_dir / f"{sha}_{hashlib.sha1(path.encode('utf-8')).hexdigest()[:10]}.md"
        if cached.exists():
            text = cached.read_text(encoding="utf-8")
        else:
            text = self.source.raw(sha, path)
            if text is None:
                return None
            self.files_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
        doc = parse_law(text)
        with self.lock:
            self.parsed[key] = doc
        return doc


FRONT = re.compile(r"(?m)^(제목|시행일자|공포일자|상태|출처):\s*(.+?)\s*$")
ARTICLE_HEAD = re.compile(r"^#+\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^)\n]*)\))?")
PART_HEAD = re.compile(r"^#+\s*(제\s*\d+\s*(?:편|장|절|관)\b.*)$")


def parse_law(text: str) -> dict:
    """legalize-kr 법령 Markdown -> {meta, articles: {제17조: {text, parent}}, headings: [(제17조, 제목)]}.
    조문은 "##### 제17조 (제목)" 줄부터 다음 제목 줄(# 로 시작) 전까지다."""
    meta = dict(FRONT.findall(text[:3000]))
    articles, headings, part, cur, lines = {}, [], "", None, []

    def close():
        if cur:
            articles[cur[0]] = {"text": "\n".join(lines).strip(), "parent": cur[1]}

    body = text.split("\n---", 2)[-1] if text.startswith("---") else text
    for line in body.splitlines():
        if line.startswith("#"):
            m = ARTICLE_HEAD.match(line)
            if m:
                close()
                no = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
                cur, lines = (no, part), [line.lstrip("#").strip()]
                headings.append((no, (m.group(3) or "").strip()))
                continue
            close()
            cur, lines = None, []
            p = PART_HEAD.match(line)
            if p:
                part = p.group(1).strip()
            continue
        if cur:
            lines.append(line)
    close()
    return {"meta": meta, "articles": articles, "headings": headings}


def display(law: str) -> str:
    """폴더 이름을 읽기 좋게 (띄어쓰기 없이 그대로 두되 가운뎃점만 되돌린다)."""
    return law.replace("ㆍ", "·")
