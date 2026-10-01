"""RAG: 약관 질문에 조항 원문을 근거로 답한다.

검색
- 두 컬렉션: 최신 조항(clauses_{전략}, 긴 조항은 연결된 조각) + 변경 이력(changes_{전략}, 추가·삭제·수정 레코드).
- 하이브리드: 벡터(Chroma) 순위와 BM25(한글 글자 2-gram) 순위를 RRF 로 합친다. 약관 질문은 "환불", "면책" 같은
  키워드가 강해서 키워드 검색이 벡터 검색의 빈 곳을 메운다. mode="vector" / "bm25" 로 하나만 쓸 수도 있다.
- 질문 재작성(선택): LLM 이 사용자 말("해킹")을 약관 용어("제3자의 불법적인 접속")로 바꾼 검색어 몇 개를 만들고,
  원래 질문과 함께 모든 검색 결과를 RRF 로 합친다.
- 질문에 서비스 이름(쿠팡, 롤, 배그 …)이 있으면 그 회사로 거른다.
- 걸린 조각은 index.expand 로 조항 전체(큰 조항은 앞뒤 조각)로 되돌린다.

질문 분기 (route)
- LLM 한 번으로 질문을 현재 약관 / 조항 변경 이력 / 문서 개정 이력으로 나누고, 검색어와 기간을 뽑는다 (날짜는 코드로도 뽑아 우선).
- 조항 이력: 조항을 검색으로 찾은 뒤, 그 조항의 변경 이력 전체를 변경 이력 DB(db.py)에서 조회해 기간으로 좁힌다.
  이미 삭제된 조항은 현재 조항 검색에 안 걸리므로 변경 이력 검색도 함께 한다.
- 문서 이력: 찾은 문서의 개정일 목록을 DB 에서 가져온다.

평가
- qa-draft: 조항·변경 레코드에서 LLM 으로 질문 초안을 만든다 (reports/qa_sheet.csv) → 피드백 UI 에서 채택·수정·버림.
- rag-eval: 채택한 질문으로 recall@k, MRR 을 검색 방식별로 잰다.

생성
- answer: 검색한 조항·변경 이력을 번호 붙은 근거로 주고, 근거 안에서만 답하며 [C1] [H2] 처럼 인용하게 한다.
"""
import json
import math
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import index as I
from . import laws as L
from . import services as S
from .normalize import content_hash

LLM_MODEL = "gpt-5.4-mini"
RRF_K = 60
BM25_WEIGHT = 0.5              # 하이브리드에서 BM25 순위의 RRF 가중치 (벡터는 1). 사용자 말투 질문(qa-vague)에서
                               # BM25 가 순위를 흐트러뜨려 낮췄다. 원래 질문 성적은 1.0 과 같고 모호한 질문이 가장 좋았다.
CHANGE_SEARCH = "always"       # route 의 변경 검색. fallback(이력이 없거나 날짜·삭제 질문일 때만)은 이력 29문항 중
                               # 3문항을 잃었다 (조항 이력이 엉뚱한 조항을 잡았을 때 변경 검색이 정답을 찾아 준다).
N_TIMELINES = 2                # 조항 이력을 붙일 상위 조항 수. 3→2→1 모두 이력 근거 포함률이 같아(0.966) 근거를 줄였다
_GONE_WORDS = re.compile(r"없어|삭제|폐지|사라|빠졌|빠진|예전에 있던|아직도|아직 있|지금도 있|지금도 남아|남아 있")
# 분기 LLM 이 추론한 문서 종류로 순위 올리기. 평가에서 오히려 떨어져(개인위치정보·채팅 질문을 개인정보처리방침으로 끌어감) 끈다.
DOC_TYPE_BOOST = False
CANDIDATES = 50            # 방식마다 가져올 조각 후보 수
DIVERSE_MIN = 2            # 회사를 가리지 않은 검색: 회사당 최소 상위 2개. 1개면 회사 안에서 엉뚱한 조항이
                           # 1위일 때 그 회사가 통째로 빠진다 ("회원 탈퇴" → 쿠팡 멤버십 전문이 1위, 이용약관 제7조는 2위)
DIVERSE_K = 5              # k 가 이만큼 늘 때마다 회사당 1개씩 더 (k=15 → 3)
# "전체 회사" 검색에서 뺄 문서: 서비스 수준 협약(SLA). 네이버 클라우드 플랫폼에만 77개가 있고 틀이 거의 같아서,
# 전체 순위와 그 회사 몫을 SLA 가 차지한다. 회사를 고른 검색이나 SLA 를 직접 묻는 질문에서는 빼지 않는다.
SLA_TITLE = re.compile(r"서비스\s*수준\s*협약|\bSLA\b", re.I)
SLA_WORDS = re.compile(r"SLA|서비스\s*수준|가용\s*(률|성)|가동\s*률", re.I)



def detect_company(query: str) -> str | None:
    """질문 속 서비스 이름 -> 회사 (services.detect 의 회사 부분만)."""
    w = S.detect(query)
    if "company" in w:
        return w["company"] if isinstance(w["company"], str) else w["company"][0]
    return w["service"][0].split("/")[0] if w else None


# ---------------------------------------------------------------------------
# BM25 (한글은 띄어쓰기가 흔들리므로 공백을 뺀 글자 2-gram + 영숫자 단어)
# ---------------------------------------------------------------------------
def tokens(text: str) -> list[str]:
    t = text.lower()
    words = re.findall(r"[a-z0-9]+", t)
    han = re.sub(r"[^가-힣]", "", t)
    return words + [han[i:i + 2] for i in range(len(han) - 1)]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokens(d)) for d in docs]
        self.len = [sum(tf.values()) for tf in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df = Counter(t for tf in self.tf for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str, allowed: list[int] | None = None) -> list[tuple[int, float]]:
        q = set(tokens(query))
        out = []
        for i in (allowed if allowed is not None else range(len(self.tf))):
            tf, s = self.tf[i], 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                out.append((i, s))
        return sorted(out, key=lambda x: -x[1])


# ---------------------------------------------------------------------------
# 검색
# ---------------------------------------------------------------------------
class Corpus:
    """한 컬렉션의 조각들 (로컬 사본). BM25 와 조항 복원(expand)은 로컬에서, 벡터 검색만 Chroma 에서."""

    def __init__(self, entries: list[dict], db_dir: Path, name: str, embedder):
        self.entries, self.db_dir, self.name, self.embedder = entries, db_dir, name, embedder
        self.by_id = {e["id"]: e for e in entries}
        self.by_group: dict[str, list[dict]] = {}
        for e in entries:
            self.by_group.setdefault(e["meta"]["group"], []).append(e)
        self.bm25 = BM25([e["embed"] for e in entries])
        self.pos = {e["id"]: i for i, e in enumerate(entries)}
        # 같은 내용 묶기용 키: 플랫폼별로 텍스트가 같은 조항(PUBG Steam·Xbox …)이나 같은 날 같은 변경
        self.dup_key = {g: _dup_key(es) for g, es in self.by_group.items()}
        self.paths = sorted({e["meta"]["path"] for e in entries})
        self._col = None

    def col(self):
        if self._col is None:
            self._col = I._collection(self.db_dir, self.name, self.embedder.model)
        return self._col

    def _match(self, meta: dict, where: dict) -> bool:
        """where 값: 목록이면 그중 하나, {"$nin": [...]} 이면 그 밖, 아니면 같은 값."""
        def ok(x, v):
            if isinstance(v, dict):
                return x not in v["$nin"]
            return x in v if isinstance(v, list) else x == v
        return all(ok(meta.get(k), v) for k, v in where.items())

    def _runs(self, queries: list[str], vecs, where: dict, mode: str) -> list[list[str]]:
        runs = []
        if any(v == [] for v in where.values()):          # 남는 값이 없는 조건 (_and 로 다 빠진 경우)
            return runs
        if mode in ("vector", "hybrid"):
            cs = [{k: {"$in": v}} if isinstance(v, list) else {k: v} for k, v in where.items()]   # $nin 은 그대로
            w = None if not cs else (cs[0] if len(cs) == 1 else {"$and": cs})
            r = self.col().query(query_embeddings=vecs, n_results=min(CANDIDATES, len(self.entries)), where=w,
                                 include=[])
            runs += r["ids"]
        if mode in ("bm25", "hybrid"):
            allowed = [i for i, e in enumerate(self.entries) if self._match(e["meta"], where)] if where else None
            if allowed == []:
                return runs
            runs += [([self.entries[i]["id"] for i, _ in self.bm25.scores(q, allowed)[:CANDIDATES]], BM25_WEIGHT)
                     for q in queries]
        return runs

    def ranked(self, queries: list[str], vecs, where: dict, mode: str, boosts: list[dict] | None = None) -> list[str]:
        """조각 id 순위 (mode: vector | bm25 | hybrid). 검색어가 여럿이면 검색어마다의 순위를 모두 합친다.
        boosts: 우선할 조건들 (예: {"path": [...]}, {"doc_type": "개인정보처리방침"}). 거르지 않고, 그 조건으로 한 번 더
        찾은 순위를 RRF 에 더해 순위만 올린다."""
        runs = self._runs(queries, vecs, where, mode)
        for b in boosts or []:
            runs += self._runs(queries, vecs, _and(where, b), mode)
        return _rrf(runs)

    def ranked_within(self, queries: list[str], vecs, ids: list[str], mode: str) -> list[str]:
        """주어진 조각들(예: DB 에서 기간으로 고른 변경) 안에서만 순위를 매긴다. 벡터 유사도는 저장된 임베딩으로 직접 계산."""
        ids = [i for i in ids if i in self.pos]
        if not ids:
            return []
        runs = []
        if mode in ("bm25", "hybrid"):
            idx = [self.pos[i] for i in ids]
            runs += [([self.entries[i]["id"] for i, _ in self.bm25.scores(q, idx)], BM25_WEIGHT) for q in queries]
        if mode in ("vector", "hybrid") and vecs is not None:
            import numpy as np
            got = self.col().get(ids=ids, include=["embeddings"])
            m = np.asarray(got["embeddings"], dtype=float)
            m /= np.linalg.norm(m, axis=1, keepdims=True)
            for v in vecs:
                sims = m @ (np.asarray(v, dtype=float) / np.linalg.norm(v))
                runs.append([got["ids"][i] for i in np.argsort(-sims)])
        return _rrf(runs)

    def search(self, queries: str | list[str], vecs, k: int, where: dict, mode: str, with_refs: bool = False,
               within: list[str] | None = None, boosts: list[dict] | None = None) -> list[dict]:
        queries = [queries] if isinstance(queries, str) else queries
        ids = (self.ranked_within(queries, vecs, within, mode) if within is not None
               else self.ranked(queries, vecs, where, mode, boosts))
        # 같은 내용(플랫폼별 같은 약관 등)은 먼저 걸린 하나만 남기고, 나머지 문서는 also_in 으로 붙인다
        order, seen_key, also = [], {}, {}
        for g in dict.fromkeys(self.by_id[i]["meta"]["group"] for i in ids if i in self.by_id):
            key = self.dup_key[g]
            if key in seen_key:
                also.setdefault(seen_key[key], []).append(g)
                continue
            if len(order) < k:
                seen_key[key] = g
                order.append(g)
        keep = set(order)
        hits = [{"id": i, "text": self.by_id[i]["text"], "meta": self.by_id[i]["meta"], "similarity": round(1 / (n + 1), 3)}
                for n, i in enumerate(ids) if i in self.by_id and self.by_id[i]["meta"]["group"] in keep]

        def fetch(w):
            want = set(w["group"]["$in"])
            return [e for g in want for e in self.by_group.get(g, [])]

        out = I.expand(hits, fetch, with_refs)
        for rank, v in enumerate(out):
            v["rank"] = rank + 1
            v["also_in"] = [{"group": g, "path": self.by_group[g][0]["meta"]["path"]} for g in also.get(v["group"], [])]
        return out


    def search_diverse(self, queries: list[str], vecs, k: int, mode: str, with_refs: bool = False,
                       within: list[str] | None = None, boosts: list[dict] | None = None,
                       exclude: dict | None = None) -> list[dict]:
        """회사를 가리지 않은 검색 ("전체 회사에서 찾기", "어느 회사가 환불이 쉬워?"): 회사마다 상위 조각을 따로 모은다.
        전체 순위로 k 개를 자르면 표현이 가까운 한 회사가 결과를 다 차지하기 때문이다.
        회사마다 k//5 개(최소 DIVERSE_MIN), 회사 순서는 전체 순위에서 그 회사가 처음 나온 자리.
        exclude: 아예 뺄 조각의 조건 (예: {"path": {"$nin": SLA 문서}}, Retriever.excluded)."""
        exclude = exclude or {}
        if within is not None and exclude:
            within = [i for i in within if i in self.by_id and self._match(self.by_id[i]["meta"], exclude)]
        order = (self.ranked_within(queries, vecs, within, mode) if within is not None
                 else self.ranked(queries, vecs, exclude, mode, boosts))
        first: dict[str, int] = {}
        for pos, i in enumerate(order):
            if i in self.by_id:
                first.setdefault(self.by_id[i]["meta"]["company"], pos)
        pool = within if within is not None else [e["id"] for e in self.entries if self._match(e["meta"], exclude)]
        by_company: dict[str, list[str]] = {}
        for i in pool:
            if i in self.by_id:
                by_company.setdefault(self.by_id[i]["meta"]["company"], []).append(i)
        per = max(DIVERSE_MIN, k // DIVERSE_K)
        out = []
        for c in sorted(by_company, key=lambda c: first.get(c, len(order))):
            if c not in first:                     # 이 회사는 어떤 검색어에도 걸리지 않았다
                continue
            if within is not None:
                got = self.search(queries, vecs, per * 3, {}, mode, with_refs, within=by_company[c])
            else:
                got = self.search(queries, vecs, per * 3, {"company": c, **exclude}, mode, with_refs, boosts=boosts)
            # 플랫폼별 같은 조항(PUBG Steam·PlayStation 제19조)은 문구가 조금 달라도 회사 몫을 하나만 쓴다
            kept: dict[str, dict] = {}
            for v in got:
                key = _platform_free(v)
                if key in kept:
                    kept[key].setdefault("also_in", []).append({"group": v["group"], "path": v["path"]})
                elif len(kept) < per:
                    kept[key] = v
            out += kept.values()
        for rank, v in enumerate(out):
            v["rank"] = rank + 1
        return out


def _platform_free(v: dict) -> str:
    """플랫폼 괄호를 뗀 조항 키: 'krafton/pubg/서비스이용약관(Steam).md::제19조' -> '…/서비스이용약관.md::제19조' (+ 변경일·유형)."""
    base = re.sub(r"\([^)]*\)\.md$", ".md", v["path"])
    return "::".join([base, v["clause_id"], v.get("version_date", "") if "change_type" in v else "", v.get("change_type", "")])


def _dup_key(es: list[dict]) -> str:
    m = es[0]["meta"]
    body = "\n".join(e["text"] for e in sorted(es, key=lambda e: e["meta"]["piece"]))
    if "change_type" in m:                  # 변경: 머리말(문서 이름)을 빼고 날짜·유형·내용이 같으면 같은 변경
        body = m["version_date"] + m["change_type"] + body.split("\n", 1)[-1]
    return content_hash(body)


def _and(where: dict, extra: dict) -> dict:
    """두 조건을 모두 만족: 같은 키가 목록과 {"$nin": …} 이면 목록에서 뺀 것으로 (순위 올리기가 제외를 덮지 않게)."""
    out = dict(where)
    for k, v in extra.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, list):
            out[k] = [x for x in v if x not in cur["$nin"]]
        elif isinstance(v, dict) and isinstance(cur, list):
            out[k] = [x for x in cur if x not in v["$nin"]]
        else:
            out[k] = v
    return out


def _rrf(runs: list) -> list[str]:
    """Reciprocal Rank Fusion: 여러 순위를 합친다. 순위는 id 목록 또는 (id 목록, 가중치)."""
    runs = [r if isinstance(r, tuple) else (r, 1.0) for r in runs]
    if len(runs) == 1:
        return runs[0][0]
    fused: dict[str, float] = {}
    for run, weight in runs:
        for rank, i in enumerate(run):
            fused[i] = fused.get(i, 0.0) + weight / (RRF_K + rank + 1)
    return sorted(fused, key=lambda i: -fused[i])


class Retriever:
    def __init__(self, clauses: list[dict], records: list[dict], db_dir: Path, strategy: str, embedder, db=None):
        self.embedder, self.db = embedder, db
        ces = I.change_entries(records)
        self.clauses = Corpus(I.entries(clauses), db_dir, I.collection_name("clauses", strategy, embedder.model), embedder)
        self.changes = Corpus(ces, db_dir, I.collection_name("changes", strategy, embedder.model), embedder)
        # 평가용 동등 키: 플랫폼별로 내용이 같은 문서(PUBG Steam·Xbox …)의 같은 조항·같은 변경은 같은 정답으로 본다
        self.equiv = {I.group_id(c): content_hash(c["text"]) for c in clauses}
        self.equiv.update({e["id"]: e["meta"]["version_date"] + content_hash(e["text"].split("\n", 1)[-1]) for e in ces})
        # 평가용 계보: 변경·현재 조항이 같은 조항(계보)인지. 이력 질문이 사실상 현재 내용을 물을 때 "조항은 맞혔다"를 본다.
        self.lineage = {I.group_id(c): f"{c['path']}::{c.get('lineage')}" for c in clauses}
        self.lineage.update({e["id"]: f"{e['meta']['path']}::{e['meta']['lineage']}" for e in ces})
        self.sla_paths = sorted({e["meta"]["path"] for c in (self.clauses, self.changes) for e in c.entries
                                 if SLA_TITLE.search(e["meta"].get("doc_title") or "")})

    def excluded(self, query: str) -> dict:
        """전체 회사 검색에서 뺄 조각 조건: SLA 문서. 질문이 SLA·서비스 수준·가용률을 물으면 빼지 않는다."""
        if not self.sla_paths or SLA_WORDS.search(query):
            return {}
        return {"path": {"$nin": self.sla_paths}}

    def where(self, query: str, company: str | None = None, doc_type: str | None = None, auto_company: bool = True) -> dict:
        """필터: 명시한 회사·서비스(--service), 없으면 질문 속 서비스 이름 (services.detect)."""
        if company:
            w = ({"service": [company, company.split("/")[0]]} if "/" in company else {"company": company})
        else:
            w = S.detect(query) if auto_company else {}
        if doc_type:
            w["doc_type"] = doc_type
        return w

    def boosts(self, query: str, plan: dict | None = None) -> list[dict]:
        """거르지 않고 순위만 올릴 조건: 질문 속 문서·플랫폼 이름 ("쿠팡이츠", "스팀"), 분기 LLM 이 추론한 문서 종류."""
        out = []
        paths = [p for frag in S.preferred_docs(query) for p in self.clauses.paths if frag in p]
        if paths:
            out.append({"path": sorted(set(paths))})
        if plan and plan.get("doc_type") and DOC_TYPE_BOOST:
            out.append({"doc_type": plan["doc_type"]})
        return out

    def _named_doc(self, query: str, where: dict) -> str | None:
        """문서 개정 질문이 가리키는 문서: 제목 글자 2-gram 이 질문에 가장 많이 들어 있는 문서.
        동점이면 하위 서비스를 짚지 않은 질문("라이엇 약관")은 회사 공통 문서(riotgames/서비스약관.md)를 고른다."""
        q = set(tokens(query))
        best = None
        for d in self.db.documents():
            if not all(d.get(k) in v if isinstance(v, list) else d.get(k) == v for k, v in where.items()):
                continue
            t = tokens(d["title"])
            score = len(q & set(t)) / len(set(t)) if t else 0.0
            key = (score, d["service"] == d["company"], -len(t))
            if score > 0 and (best is None or key > best[0]):
                best = (key, d["path"])
        return best[1] if best else None

    def search(self, query: str, k: int = 5, k_changes: int = 5, mode: str = "hybrid", company: str | None = None,
               doc_type: str | None = None, auto_company: bool = True, with_refs: bool = True,
               rewrite: "LLM | None" = None) -> dict:
        queries = [query] + (rewrite_query(query, rewrite) if rewrite else [])
        vecs = self.embedder.query(queries) if mode != "bm25" else None
        w = self.where(query, company, doc_type, auto_company)
        b = self.boosts(query)
        return {"where": w, "queries": queries,
                "clauses": self.clauses.search(queries, vecs, k, w, mode, with_refs, boosts=b) if k else [],
                "changes": self.changes.search(queries, vecs, k_changes, w, mode, boosts=b) if k_changes else []}

    def route(self, query: str, llm: "LLM | None" = None, plan: dict | None = None, k: int = 5, k_changes: int = 5,
              mode: str = "hybrid", company: str | None = None, doc_type: str | None = None, auto_company: bool = True,
              with_refs: bool = True, n_timelines: int = N_TIMELINES, change_search: str = CHANGE_SEARCH,
              diversify: bool | None = None) -> dict:
        """질문 분기에 따라 검색 + DB 조회.

        diversify: 회사마다 상위 결과를 따로 모은다 (Corpus.search_diverse). None 이면 거르는 조건이 하나도 없을 때
        (회사를 고르지도, 질문에 서비스 이름을 쓰지도 않았을 때) 켠다.

        change_search: "always" 면 이력 질문마다 변경 검색도 한다. "fallback" 이면 조항 이력(DB)을 기본으로 하고,
        변경 검색은 이력이 안 잡혔거나, 날짜를 짚었거나, 삭제·폐지를 물을 때만 한다 (이미 삭제된 조항은 현재 조항
        검색에 걸리지 않으므로)."""
        plan = plan or plan_query(query, llm)
        d_from, d_to = plan["date_from"], plan["date_to"]
        queries = [query] + plan["queries"]
        vecs = self.embedder.query(queries) if mode != "bm25" else None
        w = self.where(query, company, doc_type, auto_company)
        b = self.boosts(query, plan)
        diverse = not w if diversify is None else diversify
        x = self.excluded(query) if diverse else {}
        clauses = (self.clauses.search_diverse(queries, vecs, k, mode, with_refs, boosts=b, exclude=x) if diverse
                   else self.clauses.search(queries, vecs, k, w, mode, with_refs, boosts=b))
        res = {"plan": plan, "where": w, "queries": queries, "timelines": [], "changes": [], "doc_versions": [],
               "clauses": clauses, "diverse": diverse, "sla_excluded": bool(x)}
        if plan["intent"] == "current":
            return res

        md = plan.get("month_day")
        dated = bool(d_from or d_to or md)

        def in_range(date):
            return (not d_from or date >= d_from) and (not d_to or date <= d_to) and (not md or date[5:].startswith(md))

        seen, lineages = set(), set()

        def add_timeline(v, lineage=None):
            full = self.db.timeline(v["path"], v["clause_id"], lineage=lineage)
            if not full or f"{v['path']}::{full[0]['lineage']}" in lineages:
                return
            lineages.add(f"{v['path']}::{full[0]['lineage']}")
            hit = [c for c in full if in_range(c["version_date"])] if dated else full
            res["timelines"].append({"path": v["path"], "clause_id": v["clause_id"], "title": v["title"],
                                     "doc_title": v["doc_title"], "total": len(full), "date_matched": bool(hit),
                                     "changes": hit or full})
            seen.update(c["id"] for c in hit or full)

        if self.db:
            for v in res["clauses"][:n_timelines]:
                add_timeline(v)
        # 삭제된 조항 등 현재 조항 검색에 안 걸리는 변경: 변경 이력 검색.
        # 기간이 있으면 DB 에서 그 기간(과 회사)의 변경만 후보로 뽑아 그 안에서 순위를 매긴다.
        need = change_search == "always" or not res["timelines"] or dated or bool(_GONE_WORDS.search(query))
        if not need:
            hits = []
        elif dated and self.db:
            cands = [c["id"] for c in self.db.changes_between(w.get("company"), None, d_from, d_to, limit=2000,
                                                              month_day=md)]
            hits = (self.changes.search_diverse(queries, vecs, k_changes, mode, within=cands, exclude=x) if diverse
                    else self.changes.search(queries, vecs, k_changes + len(seen), w, mode, within=cands))
        elif diverse:
            hits = self.changes.search_diverse(queries, vecs, k_changes, mode, boosts=b, exclude=x)
        else:
            hits = self.changes.search(queries, vecs, k_changes + len(seen), w, mode, boosts=b)
        if self.db and not dated:
            # 날짜 없는 질문은 같은 조항의 여러 버전이 서로 경쟁한다. 변경 검색 상위의 조항은 계보 전체 이력을 붙인다
            # (삭제된 조항이면 삭제까지, 여러 번 바뀐 조항이면 모든 버전).
            for h in hits[:n_timelines]:
                add_timeline(h, h.get("lineage") or None)
        hits_new = [h for h in hits if h["group"] not in seen]
        res["changes"] = hits_new if diverse else hits_new[:k_changes]     # 회사별로 모았으면 뒤 회사를 자르지 않는다
        if plan["intent"] == "doc_history" and self.db:
            named = self._named_doc(query, w)
            paths = list(dict.fromkeys(([named] if named else []) + [v["path"] for v in res["clauses"][:2]]
                                       + [h["path"] for h in hits[:1]]))
            for p in paths[:2]:
                doc = self.db.document(p)
                if doc:
                    vs = self.db.doc_versions(p)
                    res["doc_versions"].append({**doc, "versions": vs,
                                                "in_range": [x for x in vs if in_range(x["version_date"])]})
        return res


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLM:
    def __init__(self, model: str = LLM_MODEL):
        from openai import OpenAI

        self.client, self.model = OpenAI(max_retries=8), model   # 분당 토큰 한도(429)는 기다렸다 재시도
        self.tokens = 0

    def __call__(self, system: str, user: str, json_mode: bool = False) -> str:
        r = self.client.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **({"response_format": {"type": "json_object"}} if json_mode else {}))
        self.tokens += r.usage.total_tokens
        return r.choices[0].message.content


REWRITE_SYSTEM = """너는 한국 온라인 서비스 약관 검색기의 검색어를 만든다.
사용자 질문을 약관 원문에 나올 법한 표현으로 바꾼 검색어 2~3개를 만든다.
- 일상어를 약관 용어로 바꾼다. 예: 해킹 → 제3자의 불법적인 접속, 서버의 불법적인 이용 / 환불 → 청약철회, 환급 /
  계정 정지 → 이용 제한, 이용계약 해지 / 약관 바뀜 → 약관의 변경, 개정
- 질문에 여러 궁금증이 섞여 있으면 검색어마다 하나씩 나눈다.
- "예전에 바뀐 적 있어?" 같은 변경 이력 표현은 검색어에서 뺀다 (변경 이력은 따로 검색한다).
- 서비스 이름은 빼도 된다.
JSON 으로만 답한다: {"queries": ["...", "..."]}"""


def rewrite_query(query: str, llm: "LLM") -> list[str]:
    try:
        qs = json.loads(llm(REWRITE_SYSTEM, query, json_mode=True)).get("queries", [])
    except Exception:                     # 재작성 실패는 원래 질문만으로 검색
        return []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()][:3]


# ---------------------------------------------------------------------------
# 질문 분기: 현재 약관 / 조항 변경 이력 / 문서 개정 이력
# ---------------------------------------------------------------------------
PLAN_SYSTEM = """너는 한국 온라인 서비스 약관 검색기의 질문 분석기다. 질문을 읽고 JSON 으로만 답한다.
{"intent": "current" | "clause_history" | "doc_history",
 "queries": ["...", "..."],
 "date_from": "YYYY-MM-DD" 또는 null, "date_to": "YYYY-MM-DD" 또는 null,
 "doc_type": "약관" | "운영정책" | "부가서비스" | "개인정보처리방침" | "동의서" | "대회 규정" | null}
- intent
  - current: 지금 약관 내용을 묻는다 ("환불 돼?", "책임져?")
  - clause_history: 특정 내용·조항이 언제, 어떻게 바뀌었는지 묻는다 ("면책 조항 언제 생겼어?", "예전에 바뀐 적 있어?")
  - doc_history: 문서 전체의 개정 시점을 묻는다 ("약관 마지막으로 언제 바뀌었어?", "2024년에 개정된 적 있어?")
  현재 내용과 변경 이력을 함께 물으면 clause_history.
- queries: 약관 원문에 나올 법한 표현으로 바꾼 검색어 2~3개. 일상어를 약관 용어로 바꾼다
  (해킹 → 제3자의 불법적인 접속 / 환불 → 청약철회, 환급 / 계정 정지 → 이용 제한). "언제 바뀌었어" 같은 표현과 서비스 이름은 뺀다.
- date_from/date_to: 질문이 가리키는 기간. 날짜 하나면 둘 다 그 날짜, 연도만 있으면 그해 1월 1일~12월 31일, 없으면 null.
- doc_type: 답이 있을 문서 종류가 분명할 때만 (개인정보 보관·수집·제3자 제공 → 개인정보처리방침, 제재·이용 제한 → 운영정책,
  가상 재화·유료 아이템·멤버십 → 부가서비스). 애매하면 null."""

_HISTORY_WORDS = re.compile(r"언제|바뀌|바꿨|변경|개정|예전|이전|과거|추가됐|삭제됐|생겼|없어졌|달라")
_DATE_PATTERNS = [
    (re.compile(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})"), "day"),
    (re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"), "day"),
    (re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월"), "month"),
    (re.compile(r"(\d{4})\s*년"), "year"),
]


def dates_in(query: str) -> tuple[str | None, str | None]:
    """질문 속 날짜 표현 -> (시작일, 끝일). 여러 개면 전부 덮는 기간. 코드로 뽑으므로 LLM 판단보다 우선한다."""
    import calendar
    spans, used = [], []
    for pat, unit in _DATE_PATTERNS:
        for m in pat.finditer(query):
            if any(a <= m.start() < b for a, b in used):      # 더 구체적인 패턴이 이미 잡은 자리
                continue
            y = int(m.group(1))
            if not 1990 <= y <= 2100:
                continue
            used.append(m.span())
            if unit == "day":
                mo, d = int(m.group(2)), int(m.group(3))
                spans.append((f"{y:04d}-{mo:02d}-{d:02d}",) * 2)
            elif unit == "month":
                mo = int(m.group(2))
                spans.append((f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"))
            else:
                spans.append((f"{y:04d}-01-01", f"{y:04d}-12-31"))
    if not spans:
        return None, None
    return min(s for s, _ in spans), max(e for _, e in spans)


_MONTH_DAY = re.compile(r"(?<![\d년])\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?")


def month_day_in(query: str) -> str | None:
    """연도 없이 월(일)만 짚은 날짜 -> "07-25" 또는 "07". 연도가 있는 날짜는 dates_in 이 맡는다."""
    if dates_in(query)[0]:
        return None
    m = _MONTH_DAY.search(re.sub(r"\d{2,4}\s*년\s*\d{1,2}\s*월", "", query))
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    return f"{int(m.group(1)):02d}" + (f"-{int(m.group(2)):02d}" if m.group(2) else "")


def _year_in(query: str, date: str) -> bool:
    """LLM 이 뽑은 날짜의 연도가 질문에 실제로 있는가 ("2015년", "15년"). 없으면 LLM 이 지어낸 연도다."""
    y = date[:4]
    return y in query or bool(re.search(rf"(?<!\d){y[2:]}\s*년", query))


def plan_query(query: str, llm: "LLM | None") -> dict:
    """{intent, queries, date_from, date_to, month_day}. LLM 이 없거나 실패하면 규칙으로."""
    plan = {"intent": "clause_history" if _HISTORY_WORDS.search(query) else "current", "queries": [],
            "date_from": None, "date_to": None, "month_day": None, "doc_type": None}
    if llm:
        try:
            got = json.loads(llm(PLAN_SYSTEM, query, json_mode=True))
            if got.get("intent") in ("current", "clause_history", "doc_history"):
                plan["intent"] = got["intent"]
            plan["queries"] = [q.strip() for q in got.get("queries") or [] if isinstance(q, str) and q.strip()][:3]
            plan["date_from"], plan["date_to"] = got.get("date_from"), got.get("date_to")
            if got.get("doc_type") in ("약관", "운영정책", "부가서비스", "개인정보처리방침", "동의서", "대회 규정"):
                plan["doc_type"] = got["doc_type"]
        except Exception:
            pass
    d_from, d_to = dates_in(query)
    if d_from:
        plan["date_from"], plan["date_to"] = d_from, d_to
    elif not all(_year_in(query, d) for d in (plan["date_from"], plan["date_to"]) if d):
        # 연도 없는 "7월 25일"에 LLM 이 연도를 지어 붙인다 (2025-07-25 를 2024 로). 월·일만 연도 없이 쓴다.
        plan["date_from"] = plan["date_to"] = None
    plan["month_day"] = month_day_in(query)
    if plan["intent"] == "current" and (plan["date_from"] or plan["month_day"] or _GONE_WORDS.search(query)):
        # 날짜를 콕 집거나 "아직 있어?"처럼 삭제 여부를 물으면 이력 질문으로 본다 (삭제된 조항은 현재 조항 검색에 안 걸린다)
        plan["intent"] = "clause_history"
    return plan


# ---------------------------------------------------------------------------
# 평가 질문 초안
# ---------------------------------------------------------------------------
QA_SYSTEM = """너는 한국 온라인 서비스 약관 검색 시스템의 평가 질문을 만든다.
주어진 조항(또는 조항 변경 내역)만 읽으면 답할 수 있는, 일반 사용자가 실제로 물어볼 법한 질문 하나를 만든다.
- 조항 번호, 조항 제목, 원문 표현을 그대로 베끼지 말고 사용자의 말로 쓴다.
- 어느 서비스인지 알 수 있게 서비스 이름을 자연스럽게 넣는다.
- 변경 내역이면 "언제", "어떻게 바뀌었는지"를 묻는다.
- 정의·목적·연락처처럼 사용자가 물을 일이 없는 내용이면 question 을 빈 문자열로 둔다.
JSON 으로만 답한다: {"question": "..."}"""

SERVICE_NAMES = {"coupang": "쿠팡", "krafton/pubg": "배틀그라운드(PUBG)", "kt": "KT", "tving": "티빙",
                 "nexon/mabinogimobile": "마비노기 모바일"}


def _service_name(service: str) -> str:
    if service in S.NAMES:
        return S.display_name(service)
    if service in SERVICE_NAMES:
        return SERVICE_NAMES[service]
    parts = service.split("/")
    return {"riotgames": "라이엇 게임즈"}.get(parts[0], parts[0]) + (f" {parts[1]}" if len(parts) > 1 else "")


def qa_candidates(clauses: list[dict], records: list[dict], n_current: int, n_history: int, seed: int = 5) -> list[dict]:
    """질문을 만들 대상: 서비스별로 고르게 뽑은 최신 조항 + 실제 내용이 바뀐 변경 레코드."""
    rng = random.Random(seed)

    def spread(items, n, key):
        pools: dict[str, list] = {}
        for it in items:
            pools.setdefault(key(it), []).append(it)
        pools = [rng.sample(v, len(v)) for _, v in sorted(pools.items())]
        rng.shuffle(pools)
        out = []
        while len(out) < n and any(pools):
            for p in pools:
                if p and len(out) < n:
                    out.append(p.pop())
        return out

    cur = [c for c in clauses if 80 <= len(c["text"]) <= 3000 and c["clause_id"] not in ("전문",)]
    ch = [r for r in records if r["change_type"] in ("added", "removed", "modified")
          and (r["change_type"] != "modified" or (r.get("similarity") or 1) < 0.97) and len(r["text"]) >= 60]
    picked = [{"kind": "current", "gold": I.group_id(c), "service": c["service"], "doc": c["path"],
               "clause_id": c["clause_id"], "version_date": c["version_date"], "text": c["text"][:3000]}
              for c in spread(cur, n_current, lambda c: c["service"].split("/")[0])]
    picked += [{"kind": "history", "gold": I.change_id(r), "service": r["service"], "doc": r["path"],
                "clause_id": r["clause_id"], "version_date": r["version_date"], "change_type": r["change_type"],
                "text": I.change_text(r)} for r in spread(ch, n_history, lambda r: r["service"].split("/")[0])]
    return picked


def qa_draft(cands: list[dict], llm: LLM, workers: int = 8) -> list[dict]:
    def one(c):
        prompt = f"서비스: {_service_name(c['service'])}\n문서: {c['doc']}\n\n{c['text']}"
        try:
            return json.loads(llm(QA_SYSTEM, prompt, json_mode=True)).get("question", "").strip()
        except Exception as e:            # 한 건 실패로 전체를 멈추지 않는다
            return f"(생성 실패: {type(e).__name__})"

    with ThreadPoolExecutor(workers) as ex:
        qs = list(ex.map(one, cands))
    rows = []
    for i, (c, q) in enumerate(zip(cands, qs)):
        if not q:
            continue
        rows.append({"id": i + 1, "kind": c["kind"], "question": q, "gold": c["gold"], "doc": c["doc"],
                     "clause_id": c["clause_id"], "version_date": c["version_date"],
                     "change_type": c.get("change_type", ""), "text": c["text"], "판정(채택/버림)": "", "메모": ""})
    return rows


# ---------------------------------------------------------------------------
# 이력 평가 질문 초안: 유형을 고르게 (날짜 지정 / 날짜 없음 / 삭제된 조항 / 번호가 바뀐 조항 / 문서 개정)
# ---------------------------------------------------------------------------
HISTORY_TYPES = {
    "dated": "질문에 개정 날짜(YYYY년 M월 D일)를 넣고, 그날 무엇이 어떻게 바뀌었는지 묻는다.",
    "undated": "날짜 없이, 이 내용이 언제 생겼거나 언제 바뀌었는지 묻는다 (예: '…는 언제부터 생겼어?', '…가 바뀐 적 있어?').",
    "deleted": "이 조항은 지금 약관에서 없어졌다. 날짜 없이, 예전에 있던 이 내용이 언제 없어졌는지 또는 아직 있는지 묻는다.",
    "renumbered": "날짜와 조항 번호 없이, 이 내용이 그동안 어떻게 바뀌어 왔는지 묻는다.",
    "doc": "문서 전체가 언제 개정됐는지 묻는다. 주어진 버전(최신이면 '마지막으로 언제', 아니면 그 해에 개정된 적 있는지)을 가리키게 한다.",
}

QA_HISTORY_SYSTEM = """너는 한국 온라인 서비스 약관의 변경 이력 검색을 평가할 질문을 만든다.
주어진 변경 내역(또는 문서 개정 목록)을 보고, 일반 사용자가 실제로 물어볼 법한 '변경 이력' 질문 하나를 만든다.
- 반드시 '언제', '어떻게 바뀌었는지', '없어졌는지', '생겼는지'처럼 변경을 묻는다. 지금 내용만 묻는 질문은 안 된다.
- 조항 번호와 원문 표현을 그대로 베끼지 말고 사용자의 말로 쓴다. 서비스 이름을 자연스럽게 넣는다.
- 아래 [유형] 지시를 따른다.
- 변경이 띄어쓰기·번호 이동처럼 사소해서 사용자가 물을 일이 없으면 question 을 빈 문자열로 둔다.
JSON 으로만 답한다: {"question": "..."}"""


def qa_history_candidates(records: list[dict], clauses: list[dict], db, counts: dict, seed: int = 13) -> list[dict]:
    """유형별 이력 질문 대상. counts: {유형: 개수}."""
    rng = random.Random(seed)
    ch = [r for r in records if r["change_type"] != "initial"]
    current = {(c["path"], c.get("lineage")) for c in clauses}
    by_lin: dict[tuple, list[dict]] = {}
    for r in ch:
        by_lin.setdefault((r["path"], r.get("lineage")), []).append(r)

    def substantive(r):
        if r["change_type"] == "modified":
            return (r.get("similarity") or 1) < 0.95 and len(r["text"]) >= 80
        return len(r["text"]) >= 80

    def pick(pool, n, key=lambda r: r["service"].split("/")[0]):
        pools: dict[str, list] = {}
        for it in pool:
            pools.setdefault(key(it), []).append(it)
        pools = [rng.sample(v, len(v)) for _, v in sorted(pools.items())]
        rng.shuffle(pools)
        out = []
        while len(out) < n and any(pools):
            for p in pools:
                if p and len(out) < n:
                    out.append(p.pop())
        return out

    cands = []

    def add(kind, r, text):
        cands.append({"kind": "history", "htype": kind, "gold": I.change_id(r), "service": r["service"], "doc": r["path"],
                      "clause_id": r["clause_id"], "version_date": r["version_date"], "change_type": r["change_type"],
                      "text": text})

    for r in pick([r for r in ch if substantive(r) and r["change_type"] in ("modified", "added")], counts.get("dated", 0)):
        add("dated", r, I.change_text(r))
    multi = [rs for rs in by_lin.values() if len(rs) >= 2 and any(substantive(r) for r in rs)]
    for rs in pick(multi, counts.get("undated", 0), key=lambda rs: rs[0]["service"].split("/")[0]):
        r = max((r for r in rs if substantive(r)), key=lambda r: r["version_date"])
        add("undated", r, I.change_text(r))
    gone = [r for r in ch if r["change_type"] == "removed" and (r["path"], r.get("lineage")) not in current and substantive(r)]
    for r in pick(gone, counts.get("deleted", 0)):
        add("deleted", r, I.change_text(r))
    moved = [rs for rs in by_lin.values() if len({x["clause_id"].split("#")[0] for x in rs}) > 1 and any(substantive(x) for x in rs)]
    for rs in pick(moved, counts.get("renumbered", 0), key=lambda rs: rs[0]["service"].split("/")[0]):
        r = max((r for r in rs if substantive(r)), key=lambda r: r["version_date"])
        timeline = "\n\n".join(I.change_text(x)[:500] for x in sorted(rs, key=lambda x: x["version_date"]))
        add("renumbered", r, timeline)
    docs = sorted({r["path"] for r in ch})
    for path in rng.sample(docs, min(len(docs), counts.get("doc", 0))):
        vs = db.doc_versions(path)
        if len(vs) < 3:
            continue
        v = vs[-1] if rng.random() < 0.5 else rng.choice(vs[1:-1])
        doc = db.document(path)
        text = (f"문서: {doc['title']} ({path})\n가리킬 버전: {v['version_date']}"
                + (" (최신)" if v is vs[-1] else "") + "\n개정 목록: " + ", ".join(x["version_date"] for x in vs))
        cands.append({"kind": "history", "htype": "doc", "gold": f"doc:{path}::{v['version_date']}", "service": doc["service"],
                      "doc": path, "clause_id": "", "version_date": v["version_date"], "change_type": "", "text": text})
    return cands


def qa_history_draft(cands: list[dict], llm: "LLM", start_id: int, workers: int = 8) -> list[dict]:
    def one(c):
        prompt = (f"[유형] {HISTORY_TYPES[c['htype']]}\n서비스: {_service_name(c['service'])}\n문서: {c['doc']}\n\n{c['text']}")
        try:
            return json.loads(llm(QA_HISTORY_SYSTEM, prompt, json_mode=True)).get("question", "").strip()
        except Exception as e:
            return f"(생성 실패: {type(e).__name__})"

    with ThreadPoolExecutor(workers) as ex:
        qs = list(ex.map(one, cands))
    rows = []
    for c, q in zip(cands, qs):
        if not q:
            continue
        rows.append({"id": start_id + len(rows), "kind": "history", "question": q, "gold": c["gold"], "doc": c["doc"],
                     "clause_id": c["clause_id"], "version_date": c["version_date"], "change_type": c["change_type"],
                     "text": c["text"][:3000], "판정(채택/버림)": "", "메모": "", "htype": c["htype"]})
    return rows


# ---------------------------------------------------------------------------
# 모호한 질문: 정답을 보고 쓴 질문은 조항 표현이 새어 나와 너무 쉽다.
# 약관을 읽어 본 적 없는 사용자 말투로 바꾼 짝을 만들어 따로 잰다 (정답은 그대로).
# ---------------------------------------------------------------------------
VAGUE_SYSTEM = """너는 약관을 한 번도 읽어 본 적 없는 일반 사용자다. 아래 질문을 네가 실제로 검색창에 칠 법한 말로 다시 쓴다.
- 40자 안팎으로 짧게. 구어체로.
- 약관 용어를 쓰지 않는다 (청약철회, 면책, 귀책, 고의·중과실, 이용계약, 해지, 부칙, 제3자 제공, 처리방침 같은 말 금지).
  대신 "환불", "책임 안 져?", "계정 막히면", "내 정보 딴 데 넘겨?" 같은 일상어를 쓴다.
- 질문에 나온 구체적인 세부 사항(금액, 기간, 조건 목록, 항목 이름)은 빼고 궁금한 핵심만 남긴다.
- 서비스 이름은 사람들이 부르는 대로 (쿠팡, 배그, 롤, 발로, 티빙, KT …).
- 날짜가 있으면 사용자가 기억할 만한 정도로만 흐리게 ("2021년쯤", "작년 가을에"). 변경을 묻는 질문이면 변경을 묻는 뜻은 유지한다.
JSON 으로만 답한다: {"question": "..."}"""


def qa_vague(rows: list[dict], llm: "LLM", workers: int = 8) -> list[dict]:
    """채택된 원래 질문마다 모호한 짝을 만든다 (variant=vague, src_id=원래 id). 이미 짝이 있는 질문은 건너뛴다."""
    done = {r.get("src_id") for r in rows if r.get("variant") == "vague"}
    src = [r for r in rows if not r.get("variant") and (r.get("판정(채택/버림)") or "").strip() == "채택"
           and r["id"] not in done]

    def one(r):
        try:
            return json.loads(llm(VAGUE_SYSTEM, r["question"], json_mode=True)).get("question", "").strip()
        except Exception as e:
            return f"(생성 실패: {type(e).__name__})"

    with ThreadPoolExecutor(workers) as ex:
        qs = list(ex.map(one, src))
    start = max(int(r["id"]) for r in rows) + 1
    out = []
    for r, q in zip(src, qs):
        if q:
            out.append({**r, "id": start + len(out), "question": q, "variant": "vague", "src_id": r["id"],
                        "판정(채택/버림)": "", "메모": ""})
    return out


def overlap(question: str, text: str) -> float:
    """질문의 글자 2-gram 중 정답 텍스트에도 있는 비율 (질문이 정답 표현을 얼마나 베꼈나)."""
    q, t = set(tokens(question)), set(tokens(text))
    return round(len(q & t) / len(q), 3) if q else 0.0


# ---------------------------------------------------------------------------
# 검색 평가
# ---------------------------------------------------------------------------
def evaluate(rows: list[dict], retriever: Retriever, modes=("vector", "bm25", "hybrid"), ks=(1, 3, 5, 10),
             auto_company: bool = True, rewrite: "LLM | None" = None, route: "LLM | None" = None,
             route_variants: dict | None = None) -> tuple[dict, list[dict]]:
    """채택한 질문으로 방식별 recall@k, MRR. 정답은 gold(조항 group 또는 변경 id) 하나.

    내용이 같은 다른 플랫폼 문서의 조항·변경을 찾아도 정답으로 친다 (retriever.equiv).
    rewrite 를 주면 질문 재작성을 켠 방식("hybrid+rw" 등)도 함께 잰다.
    route 를 주면 질문 분기 + DB 조회("route")의 근거 포함률(context_recall)도 잰다. 이력 질문은 조항 이력과
    변경 검색 결과를 합친 근거 안에 정답 변경이 들어 있는지를 본다.
    """
    qs = [r for r in rows if (r.get("판정(채택/버림)") or "").strip() == "채택"]
    if not qs:
        return {"questions": 0}, []
    qsets = [[q["question"]] for q in qs]
    if rewrite:
        with ThreadPoolExecutor(8) as ex:
            extra = list(ex.map(lambda q: rewrite_query(q["question"], rewrite), qs))
        modes = list(modes) + [m + "+rw" for m in modes if m != "bm25"] + (["bm25+rw"] if "bm25" in modes else [])
    flat = [q["question"] for q in qs]
    vec_of = dict(zip(flat, retriever.embedder.query(flat)))
    if rewrite:
        more = sorted({x for e in extra for x in e} - set(vec_of))
        vec_of.update(zip(more, retriever.embedder.query(more) if more else []))
    K = max(ks)
    summary, detail = {"questions": len(qs), "current": sum(q["kind"] == "current" for q in qs),
                       "history": sum(q["kind"] == "history" for q in qs)}, []
    for mode in modes:
        base, rw = mode.split("+")[0], mode.endswith("+rw")
        ranks = []
        for n, q in enumerate(qs):
            queries = qsets[n] + (extra[n] if rw else [])
            corpus = retriever.clauses if q["kind"] == "current" else retriever.changes
            w = retriever.where(q["question"], auto_company=auto_company)
            res = corpus.search(queries, [vec_of[x] for x in queries] if base != "bm25" else None, K, w, base,
                                boosts=retriever.boosts(q["question"]))
            groups = [v["group"] for v in res]
            rank = _rank(groups, q["gold"], retriever.equiv)
            ranks.append(rank)
            detail.append({"mode": mode, "id": q["id"], "kind": q["kind"], "question": q["question"], "gold": q["gold"],
                           "rank": rank or "", "top1": groups[0] if groups else "", "filter": w.get("company", "")})
        for kind in ("all", "current", "history"):
            rs = [r for r, q in zip(ranks, qs) if kind == "all" or q["kind"] == kind]
            if not rs:
                continue
            m = {f"recall@{k}": round(sum(1 for r in rs if r and r <= k) / len(rs), 3) for k in ks}
            m["mrr"] = round(sum(1 / r for r in rs if r) / len(rs), 3)
            summary[f"{mode}/{kind}"] = m
    if route:
        with ThreadPoolExecutor(8) as ex:
            plans = list(ex.map(lambda q: plan_query(q["question"], route), qs))
        for vname, vkw in (route_variants or {"route": {}}).items():
            hits, sizes, clause_hits = [], [], []
            for q, plan in zip(qs, plans):
                res = retriever.route(q["question"], plan=plan, auto_company=auto_company, **vkw)
                if q["kind"] == "current":
                    ids = [v["group"] for v in res["clauses"]]
                else:
                    # 답변 근거에 실제로 들어가는 것만 센다 (조항 이력은 최근 TIMELINE_MAX 개)
                    ids = ([c["id"] for t in res["timelines"] for c in t["changes"][-TIMELINE_MAX:]]
                           + [h["group"] for h in res["changes"]])
                    # 문서 개정 질문의 정답(doc:경로::버전일)은 근거에 붙은 문서 개정 목록에서 찾는다
                    ids += [f"doc:{d['path']}::{v['version_date']}" for d in res["doc_versions"] for v in d["versions"]]
                rank = _rank(ids, q["gold"], retriever.equiv)
                hits.append(rank is not None)
                sizes.append(len(ids))
                # 조항 수준: 정답 변경과 같은 계보의 현재 조항·조항 이력·변경이 근거에 있는가
                lin = retriever.lineage.get(q["gold"])
                pool = ([v["group"] for v in res["clauses"]] + [c["id"] for t in res["timelines"] for c in t["changes"]]
                        + [h["group"] for h in res["changes"]])
                clause_hits.append(rank is not None or (lin is not None and any(retriever.lineage.get(i) == lin for i in pool)))
                detail.append({"mode": vname, "id": q["id"], "kind": q["kind"], "htype": q.get("htype", ""),
                               "question": q["question"], "gold": q["gold"],
                               "rank": rank or "", "top1": ids[0] if ids else "", "filter": res["where"].get("company", ""),
                               "intent": plan["intent"], "dates": f"{plan['date_from'] or ''}~{plan['date_to'] or ''}",
                               "context_items": len(ids)})
            for kind in ("all", "current", "history"):
                idx = [i for i, q in enumerate(qs) if kind == "all" or q["kind"] == kind]
                if idx:
                    summary[f"{vname}/{kind}"] = {"context_recall": round(sum(hits[i] for i in idx) / len(idx), 3),
                                                "clause_recall": round(sum(clause_hits[i] for i in idx) / len(idx), 3),
                                                "avg_context_items": round(sum(sizes[i] for i in idx) / len(idx), 1)}
        summary["route/intent"] = dict(Counter((q["kind"], p["intent"]) for q, p in zip(qs, plans)).most_common())
        summary["route/intent"] = {f"{a}->{b}": n for (a, b), n in summary["route/intent"].items()}
    return summary, detail


def _rank(ids: list[str], gold: str, equiv: dict) -> int | None:
    key = equiv.get(gold)
    for n, i in enumerate(ids, 1):
        if i == gold or (key is not None and equiv.get(i) == key):
            return n
    return None


# ---------------------------------------------------------------------------
# 답변 생성
# ---------------------------------------------------------------------------
# 모든 답변(약관 답변·즉시 답변)에 붙인다. 이어서 물을 거리는 화면이 추천 질문 버튼으로 따로 보여 주고,
# 답변 LLM 은 제안한 내용을 기억하지 못하므로 "원하시면 …" 은 지킬 수 없는 약속이 된다
NO_OFFER = """- "원하시면 …해 드릴게요", "더 궁금한 점 있으세요?"처럼 추가 도움을 제안하거나 되묻는 말로 끝내지 않는다.
  이어서 물을 만한 질문은 화면이 따로 보여 준다. 답변은 질문에 답한 내용에서 끝낸다."""

ANSWER_SYSTEM = """너는 한국 온라인 서비스 약관을 설명하는 도우미다. 아래 [근거]에 있는 약관 조항과 변경 이력만 사용해 답한다.
- 근거에 없는 내용은 추측하지 말고 "제공된 약관에서 찾지 못했다"고 말한다.
- 문장마다 근거 번호를 [C1], [T1], [H2], [D1], [L1] 처럼 붙인다.
  C 는 이 서비스가 마지막으로 수집한 약관 조항, T 는 조항 하나의 변경 이력(날짜순), H 는 검색으로 찾은 개별 변경, D 는 문서 전체의 개정일 목록,
  L 은 약관 조항이 "…법에 따른다"며 인용한 법령 조문(legalize-kr, 적힌 기준일에 시행 중이던 판)이다.
- 법령 조문(L)은 약관이 따르겠다고 한 내용을 풀어 줄 때 쓰고, 어느 약관 조항이 인용했는지 함께 밝힌다.
  근거에 "약관이 이 조문을 지정한 것이 아니다"라고 적힌 조문은 "인용되어 있다", "약관이 제N조를 든다"라고 쓰지 않는다.
  "약관은 ○○법을 따른다고만 하고 조문은 밝히지 않았으며, 관련 조문으로는 ○○법 제N조(제목)가 있다"처럼 구분해서 쓴다.
  약관과 법령이 다르게 읽히면 둘 다 그대로 전하고 어느 쪽이 우선하는지는 판단하지 않는다. 법률 자문처럼 말하지 않는다.
- 질문한 기간에 변경이 없다고 적혀 있으면 그대로 말하고, 가장 가까운 변경을 알려 준다.
- 근거의 버전 날짜는 이 서비스가 마지막으로 수집한 버전이고, 회사가 지금 게시한 약관과 다를 수 있다.
  "현재 버전은 …이다", "현행 약관은 …이다"처럼 단정하지 말고 "제가 알고 있는 버전(…)에서는"처럼 쓴다.
  버전 안내 문구("최신이 아닐 수 있다")는 답변 끝에 따로 붙으므로 직접 쓰지 않는다.
- 날짜는 근거에 적힌 그대로 쓴다. 변경 이력을 말할 때는 개정일(과 시행일)을 밝힌다.
- 여러 서비스가 섞여 있으면 서비스별로 나눠 답한다.
- 조항을 말할 때는 문서 이름을 함께 쓴다 (예: "쿠팡 이용약관 제7조"). 조 번호가 같아도 다른 문서의 조항은 섞지 않는다.
- 쉬운 말로, 짧게 답한다. 사용자에게 불리할 수 있는 조건(환불 제한, 면책, 일방적 변경 등)이 있으면 분명히 짚는다.
""" + NO_OFFER


def context_blocks(res: dict) -> tuple[str, list[dict]]:
    blocks, cites = [], []
    for n, v in enumerate(res["clauses"], 1):
        tag = f"C{n}"
        head = f"[{tag}] {S.doc_label(v)} {v['clause_id']} {v['title']} (수집본 {v['version_date']} 버전)"
        if v.get("also_in"):
            head += " · 같은 내용: " + ", ".join(S.platform(a["path"]) or a["path"] for a in v["also_in"])
        body = v["text"] + "".join(f"\n\n(참조 {r['clause_id']} {r['title']})\n{r['text'][:1500]}" for r in v.get("related", []))
        blocks.append(head + "\n" + body)
        cites.append({"tag": tag, "path": v["path"], "clause_id": v["clause_id"], "version_date": v["version_date"],
                      "doc": S.doc_label(v)})
    for n, t in enumerate(res.get("timelines", []), 1):
        tag = f"T{n}"
        note = f"전체 {t['total']}건"
        if res.get("plan", {}).get("date_from"):
            p = res["plan"]
            note += (f", 질문한 기간({p['date_from']}~{p['date_to']}) 해당 {len(t['changes'])}건" if t["date_matched"]
                     else f", 질문한 기간({p['date_from']}~{p['date_to']})에는 변경 없음 → 전체 표시")
        lines = [f"[{tag}] {t['doc_title'] or t['path']} {t['clause_id']} {t['title']} — 변경 이력 ({note})"]
        for c in t["changes"][-TIMELINE_MAX:]:
            body = c["change_text"].split("\n", 1)[-1]
            eff = f", 시행 {c['effective_date']}" if c["effective_date"] and c["effective_date"] != c["version_date"] else ""
            lines.append(f"· {c['version_date']} 개정 ({I.CHANGE_KIND[c['change_type']]}{eff}) [{c['clause_id']}]\n"
                         + body[:TIMELINE_ITEM_CHARS])
        if len(t["changes"]) > TIMELINE_MAX:
            lines.insert(1, f"(오래된 변경 {len(t['changes']) - TIMELINE_MAX}건 생략)")
        blocks.append("\n".join(lines))
        cites.append({"tag": tag, "path": t["path"], "clause_id": t["clause_id"],
                      "version_date": ", ".join(c["version_date"] for c in t["changes"][-TIMELINE_MAX:]),
                      "doc": _doc_name(t["path"], t.get("doc_title"))})
    for n, v in enumerate(res["changes"], 1):
        tag = f"H{n}"
        blocks.append(f"[{tag}] " + v["text"])
        cites.append({"tag": tag, "path": v["path"], "clause_id": v["clause_id"], "version_date": v["version_date"],
                      "change_type": v["change_type"], "doc": _doc_name(v["path"], v.get("doc_title"))})
    for n, d in enumerate(res.get("doc_versions", []), 1):
        tag = f"D{n}"
        vs = d["versions"]
        lines = [f"[{tag}] {d['title'] or d['path']} ({d['path']}) 개정 이력: 버전 {len(vs)}개, "
                 f"최초 {d['first_version']}, 마지막 수집본 {d['latest_version']}"]
        for x in vs[-DOC_VERSIONS_MAX:]:
            eff = f", 시행 {x['effective_date']}" if x["effective_date"] and x["effective_date"] != x["version_date"] else ""
            lines.append(f"· {x['version_date']} ({'최초 수집' if x is vs[0] else '조항 ' + str(x['changed']) + '개 변경'}{eff})")
        blocks.append("\n".join(lines))
        cites.append({"tag": tag, "path": d["path"], "clause_id": "", "version_date": d["latest_version"],
                      "doc": _doc_name(d["path"], d.get("title"))})
    for n, a in enumerate(res.get("laws", []), 1):
        tag = f"L{n}"
        name = L.display(a["law"]) + ("" if a["category"] == "법률" else " " + a["category"])
        basis = "현행" if a["as_of"] == L.CURRENT else f"약관 버전 {a['as_of']} 당시"
        head = f"[{tag}] 법령 {name} {a['article']} ({basis} 시행 중인 판, 시행 {a.get('effective_date') or '미상'})"
        by = ", ".join(c["label"] for c in a.get("cited_by", []))
        how = ("(주의: 약관이 이 조문을 지정한 것이 아니다. 약관은 조 번호 없이 법령 이름만 들었고, "
               "이 조문은 조문 제목이 인용 문맥과 맞아 이 서비스가 찾은 관련 조문이다)\n" if a.get("by_title") else "")
        blocks.append(head + "\n" + (f"(인용한 약관: {by})\n" if by else "") + how + a["text"])
        cites.append({"tag": tag, "path": f"kr/{a['law']}/{a['category']}.md", "clause_id": a["article"],
                      "version_date": a.get("effective_date") or "", "source_url": a.get("source_url") or "",
                      "kind": "law"})
    return "\n\n---\n\n".join(blocks), cites


def _doc_name(path: str, title: str | None) -> str:
    """근거 문서 이름 ('쿠팡 · 이용약관'). 서비스는 문서가 든 폴더다."""
    return S.doc_label({"path": path, "service": path.rsplit("/", 1)[0], "doc_title": title or ""})


# 수집본은 회사가 지금 게시한 약관과 다를 수 있다. 빠지면 안 되는 안내라서 LLM 에 맡기지 않고 코드가 답변 끝에 붙인다
VERSION_NOTE = "이 조항은 최신이 아닐 수 있습니다."


def version_note(cites: list[dict], db=None) -> str:
    """답변이 인용한 약관 문서마다 이 서비스가 아는 최신 버전(마지막으로 수집한 개정일)을 밝히는 안내. 법령(L)은 뺀다.
    날짜는 이력 DB 의 latest_version, DB 가 없으면 현재 조항(C)·문서 개정 목록(D) 근거의 날짜.
    T·H 근거의 날짜는 과거 변경일이라 최신 버전으로 쓰지 않는다."""
    docs: dict[str, tuple[str, str]] = {}
    for c in cites:
        if c.get("kind") == "law" or not c.get("path"):
            continue
        d = db.document(c["path"]) if db else None
        date = (d or {}).get("latest_version") or (c.get("version_date", "") if c["tag"][0] in "CD" else "")
        name = c.get("doc") or (_doc_name(c["path"], d["title"]) if d else c["path"])
        if c["path"] not in docs or (date and not docs[c["path"]][1]):
            docs[c["path"]] = (name, date)
    if not docs:
        return ""
    items = [f"{name} {date} 버전" if date else name for name, date in docs.values()]
    if len(items) == 1:
        return f"※ 현재 제가 알고 있는 버전은 {items[0]}입니다. {VERSION_NOTE}"
    return ("※ 현재 제가 알고 있는 버전은 다음과 같습니다. 이 조항들은 최신이 아닐 수 있습니다.\n"
            + "\n".join("· " + i for i in items))


def with_version_note(text: str, cites: list[dict], used: set[str], db=None) -> str:
    """답변 끝에 버전 안내를 붙인다. 인용한 근거 기준, 인용이 없으면 답변에 쓴 근거 전부."""
    note = version_note([c for c in cites if c["tag"] in used] or cites, db)
    return text.rstrip() + "\n\n" + note if note else text


TIMELINE_MAX = 8              # 조항 이력 하나에 넣을 최대 변경 수 (최근 것부터)
TIMELINE_ITEM_CHARS = 700
DOC_VERSIONS_MAX = 30


def answer(question: str, retriever: Retriever, llm: LLM, rewrite: bool = True, route: bool = True, **search_kw) -> dict:
    """route 면 질문 분기 + DB 조회, 아니면 현재 조항 + 변경 검색 (rewrite 로 재작성만)."""
    if route and retriever.db:
        res = retriever.route(question, llm if rewrite else None, **search_kw)
    else:
        res = retriever.search(question, rewrite=llm if rewrite else None, **search_kw)
    ctx, cites = context_blocks(res)
    text = llm(ANSWER_SYSTEM, f"[근거]\n{ctx}\n\n[질문]\n{question}")
    used = set(re.findall(r"\[([CTHDL]\d+)\]", text))
    text = with_version_note(text, cites, used, getattr(retriever, "db", None))
    return {"answer": text, "citations": [c for c in cites if c["tag"] in used], "retrieved": cites,
            "filter": res["where"], "queries": res["queries"], "plan": res.get("plan")}
