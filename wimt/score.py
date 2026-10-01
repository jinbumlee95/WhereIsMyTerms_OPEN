"""조항별 사용자 유불리 지수 (Jev score).

척도는 5단계(매우 불리 · 불리 · 중립 · 유리 · 매우 유리). 채점기는 단계별 확률을 내고,
기대값(−2 ~ +2)을 지수로, 가장 높은 확률을 신뢰도로 코드에서 계산한다.

채점기
- JevScorer: typesafe.ai Jev API. 단계마다 noul 질문 하나씩, 조항 텍스트와 문서 종류를 state 로 전달.
  TYPESAFE_API_KEY 가 있어야 동작한다.
- BaselineScorer: 키워드 규칙 기준선. Jev 없이 파이프라인 전체를 돌려 보고 테스트하기 위한 것이며,
  유불리 판단의 품질을 대표하지 않는다.
- CachedScorer: (채점기, 문서 종류, 조항 텍스트) 해시로 결과를 저장해 바뀐 조항만 새로 채점한다.
  수탁사·대리점 목록처럼 긴 목록 항은 채점 대상이 아니다 (skip_list, 채점기를 부르지 않는다).
"""
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .normalize import content_hash, normalize

LEVELS = [(-2, "매우 불리"), (-1, "불리"), (0, "중립"), (1, "유리"), (2, "매우 유리")]


@dataclass
class Score:
    favor_score: float          # 기대값, −2 ~ +2
    favor_confidence: float     # 가장 높은 단계의 확률
    probs: list[float]          # LEVELS 순서의 확률
    scorer: str


def from_probs(probs: list[float], scorer: str) -> Score:
    total = sum(probs)
    p = [x / total for x in probs] if total > 0 else [0.0, 0.0, 1.0, 0.0, 0.0]
    expected = sum(pi * v for pi, (v, _) in zip(p, LEVELS))
    return Score(round(expected, 3), round(max(p), 3), [round(x, 4) for x in p], scorer)


@dataclass
class Item:
    """채점할 조항 하나."""
    service: str
    doc_type: str
    title: str
    text: str


# ---------------------------------------------------------------------------
# Jev
# ---------------------------------------------------------------------------
# 유불리 지수를 보여 주는 곳(CLI 출력, alerts.md, 이용 안내, README)에 항상 붙이는 문구
DISCLAIMER = ("유불리 지수는 AI 판정 모델(typesafe.ai Jev)이 자동으로 매긴 참고용 점수입니다. "
              "법적 판단이 아니며, 약관이나 기업의 적법성·공정성을 평가하지 않습니다. 효력을 가지는 정본은 각 기업의 공식 약관입니다.")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"        # 기준값을 조정한 뒤에는 버전을 고정한다
JEV_BATCH = 8                   # 조항 8개 × 5단계 = 질문 40개/호출
SNIPPET_CHARS = 800

# 단계별 판정 기준. 거짓 쪽에 "단어만 겹침"을 적어 키워드만으로 높은 점수를 주지 않게 한다.
LEVEL_CRITERIA = {
    -2: ("The clause severely disadvantages the user: it waives or broadly limits the company's liability, "
         "lets the company unilaterally take away the user's money, content or rights, or blocks refunds and objections.",
         "The clause does not severely disadvantage the user; merely mentioning liability or restrictions is not enough."),
    -1: ("The clause somewhat disadvantages the user compared with a neutral rule: it adds user duties, "
         "limits user rights, or widens the company's discretion.",
         "The clause does not put the user in a worse position than a neutral rule would."),
    0: ("The clause is neutral for the user: definitions, procedures, contact details, or restating what the law already requires.",
        "The clause clearly favors or disadvantages the user."),
    1: ("The clause gives the user a right, remedy or protection beyond a neutral rule.",
        "The clause gives the user no extra right, remedy or protection."),
    2: ("The clause strongly protects the user: the company accepts liability, guarantees refunds or compensation, "
        "or firmly restricts its own discretion.",
        "The clause does not strongly protect the user."),
}


class JevScorer:
    def __init__(self, api_key: str | None = None, model: str = JEV_MODEL):
        import httpx  # 필요할 때만

        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY 가 없습니다. --scorer baseline 으로 실행하거나 키를 설정하세요.")
        self.model = model
        self.name = f"jev:{model}"
        self.client = httpx.Client(headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30.0)

    @staticmethod
    def build_state(batch: list[Item]) -> dict:
        return {
            "service": batch[0].service,
            "clauses": [{"doc_type": it.doc_type, "title": it.title, "text": it.text[:SNIPPET_CHARS]} for it in batch],
        }

    @staticmethod
    def build_questions(batch: list[Item]) -> dict:
        qs = {}
        for i in range(len(batch)):
            for v, label in LEVELS:
                true, false = LEVEL_CRITERIA[v]
                qs[f"c{i}_{v + 2}"] = {
                    "type": "noul",
                    "instructions": f"For an ordinary user of this service, is `clauses[{i}]` '{label}' "
                                    f"(level {v:+d} on a scale from -2 very unfavorable to +2 very favorable)?",
                    "criteria": {"true": true, "false": false},
                }
        return qs

    def score(self, items: list[Item]) -> list[Score]:
        out = []
        for k in range(0, len(items), JEV_BATCH):
            batch = items[k:k + JEV_BATCH]
            r = self.client.post(JEV_URL, json={"state": self.build_state(batch), "model": self.model,
                                                "questions": self.build_questions(batch)})
            r.raise_for_status()
            answers = r.json()["answers"]
            for i in range(len(batch)):
                out.append(from_probs([answers[f"c{i}_{v + 2}"]["noul"] for v, _ in LEVELS], self.name))
        return out


# ---------------------------------------------------------------------------
# 기준선 (규칙)
# ---------------------------------------------------------------------------
# (가중치, 패턴). 한 조항에서 같은 패턴은 한 번만 센다.
# 공정위 불공정약관 시정 사례(2010–2026)에 맞췄다. 공정위가 고친 문구는 감점하고,
# 공정위가 받아들인 시정 후 문구(귀책이 있으면 책임, 민사소송법상 관할 등)는 법정 수준이므로
# 감점을 상쇄하는 만큼만 가점한다. 측정 결과는 docs/jev-ftc-eval.md.
CUES = [
    # 면책·책임 전가 (약관법 제7조). "고의 또는 중대한 과실이 없는 한"은 경과실 면책이라 감점한다
    (-2, r"책임을?\s*(지지|부담하지)\s*(않|아니)"), (-2, r"책임지지\s*않"), (-2, r"(일체의|어떠한|모든)\s*책임"),
    (-1, r"면책"), (-2, r"(중대한\s*과실|중과실)[^.。]{0,10}(이\s*)?없는\s*한"),
    (-2, r"(민\s*,?\s*형사|법적)[^.。]{0,15}(책임을?\s*묻지|문제를?\s*삼지)\s*않"), (-1, r"(모든|일체의?)\s*(손해|비용)"),
    # 환불·위약금 (제8조·제9조)
    (-2, r"환불(이|은|을)?\s*(되지\s*않|불가|하지\s*않)"), (-1, r"(반환|환급|환불)(하지|되지)\s*않"),
    (-1, r"([2-9]\d|100)\s*%[^.。]{0,10}(위약금|공제)|위약금[^.。]{0,20}([2-9]\d|100)\s*%"), (-1, r"위약벌"),
    # 이의·소송·관할·해석 (제14조·제5조)
    (-2, r"이의를?\s*제기(할\s*수\s*없|하지\s*않)"), (-2, r"소송을?\s*제기(할\s*수\s*없|하지\s*않)|소\s*제기를?\s*금지"),
    (-2, r"(본사|본점|소재지)[^.。]{0,15}관할|전속\s*관할|관할\s*법원은?\s*(갑|회사)"),
    (-2, r"(갑|회사|백화점|은행|임대인|학원)\W{0,3}의?\s*해석(에|하는\s*바에)\s*따"), (-1, r"중재"),
    # 해지·이용제한·일방 변경 (제9조·제10조)
    (-1, r"(제한|중지|정지|해지|삭제|변경|회수|거부|거절|처분)할\s*수\s*있"),
    (-1, r"(사전\s*)?(통지|통보|고지|최고)\s*없이|즉시\s*해지"), (-1, r"(폐기|임의)\s*처분"),
    (-1, r"(회사|갑)가?\s*(필요하다고\s*)?(판단|인정)하는|(회사|갑)가?\s*(정하는|지정한|지정하는)|(회사|갑)\s*소정의"),
    (-1, r"자동(으로)?\s*(연장|갱신|전환)"),
    # 의사표시 의제 (제12조)
    (-1, r"것으로\s*(간주|봅|본다|보며)"), (-1, r"간주(합|됩|한)"),
    # 법정 수준 회복
    (2, r"(회사|갑|당사)(의|는|가|에게)?\s*(고의\s*(또는|나|·|,)\s*과실|귀책\s*사유)[^.。]{0,10}없는\s*(한|경우)"),
    (1, r"(회사|갑|당사)(의|는|가|에게)?\s*(고의\s*(또는|나|·|,)\s*과실|귀책\s*사유)[^.。]{0,40}(책임을?\s*(부담|집|진|지도록)|배상)"),
    (1, r"(다만|단)\s*,?[^.。]{0,60}(그러하지\s*아니|책임을?\s*(부담|집|진))"), (1, r"민사소송법"),
    (1, r"보상(합|해)"), (1, r"환불(합니다|해\s*드립|받을\s*수\s*있)"), (1, r"청약\s*철회를?\s*할\s*수\s*있"),
    (1, r"회원은.{0,30}(요청|청구|해지|철회)할\s*수\s*있"), (1, r"이의를?\s*제기할\s*수\s*있"),
    (1, r"사전에\s*(개별\s*)?(공지|고지|통지)"),
]
_CUES = [(w, re.compile(p)) for w, p in CUES]


class BaselineScorer:
    name = "baseline-rules-v2"      # 규칙을 바꾸면 올린다 (캐시 키에 들어간다)

    def score_one(self, item: Item) -> Score:
        text = normalize(item.text)
        weights = [w for w, p in _CUES if p.search(text)]
        target = max(-2.0, min(2.0, float(sum(weights))))
        sigma = 0.7 if weights else 1.0          # 근거가 있으면 더 뾰족한 분포
        probs = [math.exp(-((v - target) ** 2) / (2 * sigma * sigma)) for v, _ in LEVELS]
        return from_probs(probs, self.name)

    def score(self, items: list[Item]) -> list[Score]:
        return [self.score_one(it) for it in items]


# ---------------------------------------------------------------------------
# 채점 제외: 긴 목록 항
# ---------------------------------------------------------------------------
# 목록이어도 짧은 항(제재 기준표, 분쟁 조항 1·2호 등, 이 코퍼스에서 최대 1,453자)은 유불리를 따질 내용이라 채점한다.
# 이보다 긴 목록은 KT 수탁사·대리점 목록(4만~14만 자)뿐이었고, 매달 이름이 바뀌어 의미 없는 채점을 되풀이했다.
LIST_SKIP_CHARS = 5000
SKIPPED = "skip:list"


def is_list(text: str) -> bool:
    """이름 나열·표 조각: 줄 대부분이 표 줄이거나, 짧은 줄(평균 25자 이하)이 15줄 이상. 의미 검색으로 얻을 게 적다."""
    lines = [s.strip() for s in text.split("\n") if s.strip()]
    if not lines:
        return False
    table = sum(s.startswith("|") for s in lines) / len(lines) >= 0.8
    return table or (len(lines) >= 15 and sum(map(len, lines)) / len(lines) <= 25)


def skip_list(it: Item) -> bool:
    """채점하지 않을 항: LIST_SKIP_CHARS 보다 긴 목록."""
    return len(it.text) > LIST_SKIP_CHARS and is_list(it.text)


def skipped_score() -> Score:
    """채점하지 않은 항의 자리 표시: 중립, 신뢰도 0. 조 점수(가장 불리한 항)를 끌어내리지 않는다."""
    return Score(0.0, 0.0, [0.0, 0.0, 1.0, 0.0, 0.0], SKIPPED)


# ---------------------------------------------------------------------------
# 캐시: 최초 수집 시 전체, 이후 바뀐 조항만 채점
# ---------------------------------------------------------------------------
class CachedScorer:
    def __init__(self, inner, path: Path):
        self.inner, self.path, self.name = inner, Path(path), inner.name
        self.cache: dict[str, dict] = {}
        self.hits = self.misses = self.skipped = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                self.cache[rec["key"]] = rec["score"]

    def key(self, it: Item) -> str:
        return content_hash(f"{self.name}\n{it.doc_type}\n{it.title}\n{it.text}")

    def score(self, items: list[Item]) -> list[Score]:
        skip = [skip_list(it) for it in items]
        self.skipped += sum(skip)
        keys = [None if sk else self.key(it) for it, sk in zip(items, skip)]
        todo = [i for i, k in enumerate(keys) if k is not None and k not in self.cache]
        self.hits += len(items) - sum(skip) - len(todo)
        self.misses += len(todo)
        if todo:
            fresh = self.inner.score([items[i] for i in todo])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for i, s in zip(todo, fresh):
                    self.cache[keys[i]] = s.__dict__
                    f.write(json.dumps({"key": keys[i], "score": s.__dict__}, ensure_ascii=False) + "\n")
        return [skipped_score() if k is None else Score(**self.cache[k]) for k in keys]


def make_scorer(kind: str, cache_dir: Path):
    inner = JevScorer() if kind == "jev" else BaselineScorer()
    return CachedScorer(inner, Path(cache_dir) / f"scores-{inner.name.replace(':', '_')}.jsonl")
