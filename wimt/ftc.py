"""공정위 불공정약관 시정 사례 (2010–2026) 를 임베딩해, 수집 조항과 문구가 비슷한 시정 사례를 찾는다.

- 사례: 공정위 보도자료(공공누리 제1유형)의 '시정 전 → 시정 후' 조항 쌍. tools/ftc_eval.py extract 가 만든
  reports/ftc_eval/pairs_all.json 과 보도자료 목록 ../ftc-unfair-terms/cases.csv 에서 읽는다 (둘 다 로컬 전용).
- 색인: 시정 전 조항을 조항 색인과 같은 임베딩 모델로 같은 Chroma 에 넣는다 (컬렉션 ftc_cases_all__{모델}).
- 연결(link): 조항 색인에 이미 저장된 조각 벡터와 사례 벡터를 행렬로 비교한다 (추가 임베딩 없음).
  조항마다 코사인 유사도 LINK_MIN 이상인 사례를 LINK_TOP 개까지 .cache/ftc_links_{전략}__{모델}.json 에 둔다.
- 쓰임: 화면에서 근거 조항 옆에 "문구가 비슷한 공정위 시정 사례"를 참고로 보여 준다. 답변 LLM 에는 넣지 않는다
  (다른 회사 사례를 근거로 이 조항이 불공정하다고 말하게 될 수 있어서). 비슷한 사례가 있다는 것은
  이 조항이 불공정하다는 판단이 아니다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from . import index as I

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ROOT / "reports" / "ftc_eval" / "pairs_all.json"
CASES_CSV = ROOT.parent / "ftc-unfair-terms" / "cases.csv"
URL = "https://www.ftc.go.kr/www/selectBbsNttView.do?key=12&bordCd=3&nttSn="
# 이 이상이면 "비슷한 사례"로 보인다. text-embedding-3-large 로 구간별 짝을 눈으로 보고 정했다 (docs/jev-ftc-eval.md):
# 0.63 이상은 대부분 같은 유형의 문구(면책끼리, '모든 손해 배상'끼리), 0.60~0.63 은 주제만 같고, 0.60 아래는 엉뚱한 짝이 섞인다.
# 유사도는 같은 주제를 잡을 뿐 불공정함을 잡지 않는다 (공정한 형태의 조항도 같은 주제의 시정 사례와 짝지어진다).
LINK_MIN = 0.63
LINK_TOP = 3
EMBED_CHARS = 2000

NOTE = ("문구가 비슷한 다른 회사의 공정거래위원회 시정 사례입니다. 이 조항이 불공정하거나 위법하다는 판단이 아니며, "
        "유사도는 AI 임베딩으로 잰 문구의 비슷한 정도입니다.")


def deleted(after: str) -> bool:
    s = re.sub(r"\W", "", after)
    return s.endswith("삭제") and len(s) < 12


def available() -> bool:
    return PAIRS.exists() and CASES_CSV.exists()


def load_cases(pairs: Path = PAIRS, cases_csv: Path = CASES_CSV) -> list[dict]:
    """시정 사례 목록. id 는 '보도자료날짜_nttSn#순번'."""
    with cases_csv.open(encoding="utf-8-sig") as f:
        meta = {r["nttSn"]: r for r in csv.DictReader(f)}
    out, n = [], {}
    for p in json.loads(pairs.read_text(encoding="utf-8")):
        sn = p["doc"].split("_")[1]
        m = meta.get(sn, {})
        n[p["doc"]] = n.get(p["doc"], 0) + 1
        after = p["after"].strip()
        out.append({"id": f"{p['doc']}#{n[p['doc']]}", "date": p["doc"][:10], "nttSn": sn,
                    "release": m.get("title", ""), "target": m.get("target", ""), "field": m.get("field", ""),
                    "action": m.get("action", ""), "law": m.get("law_articles", ""), "url": URL + sn,
                    "issue": p.get("issue", ""), "before": p["before"].strip(),
                    "after": "" if deleted(after) else after})       # 빈 값 = 조항을 통째로 지움
    return out


def entries(cases: list[dict]) -> list[dict]:
    es = []
    for c in cases:
        text = c["before"][:EMBED_CHARS]
        es.append({"id": c["id"], "text": c["before"], "embed": text,
                   "meta": {"date": c["date"], "nttSn": c["nttSn"], "issue": c["issue"], "field": c["field"],
                            "embed_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}})
    return es


def collection(model: str) -> str:
    return I.collection_name("ftc_cases", "all", model)


def links_path(cache_dir: Path, strategy: str, model: str) -> Path:
    return Path(cache_dir) / f"ftc_links_{I.collection_name('clauses', strategy, model)}.json"


def build(db_dir: Path, embedder) -> dict:
    """시정 사례를 컬렉션에 맞춘다 (바뀐 것만 임베딩)."""
    return I.upsert(entries(load_cases()), db_dir, collection(embedder.model), embedder)


def _matrix(col) -> tuple[list[str], list[dict], "object"]:
    import numpy as np
    got = col.get(include=["embeddings", "metadatas"])
    m = np.asarray(got["embeddings"], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    return got["ids"], got["metadatas"], m


def link(db_dir: Path, cache_dir: Path, strategy: str, model: str,
         min_sim: float = LINK_MIN, top: int = LINK_TOP) -> dict:
    """조항(조 단위 group)마다 비슷한 시정 사례를 찾아 저장한다. 조각이 여러 개면 조각 중 가장 비슷한 값."""
    import numpy as np
    cases = {c["id"]: c for c in load_cases()}
    cids, _, F = _matrix(I._collection(db_dir, collection(model), model))
    pids, pmeta, P = _matrix(I._collection(db_dir, I.collection_name("clauses", strategy, model), model))
    best: dict[str, dict[str, float]] = {}
    for k in range(0, len(pids), 2000):
        S = P[k:k + 2000] @ F.T
        for row, meta in zip(S, pmeta[k:k + 2000]):
            idx = np.nonzero(row >= min_sim)[0]
            if not len(idx):
                continue
            g = best.setdefault(meta["group"], {})
            for j in idx:
                g[cids[j]] = max(g.get(cids[j], 0.0), float(row[j]))
    links = {g: [[c, round(s, 3)] for c, s in sorted(v.items(), key=lambda x: -x[1])[:top]] for g, v in best.items()}
    used = {c for v in links.values() for c, _ in v}
    out = {"model": model, "strategy": strategy, "min_sim": min_sim, "top": top, "links": links,
           "cases": {c: cases[c] for c in used if c in cases}}
    path = links_path(cache_dir, strategy, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {"clauses": len({m["group"] for m in pmeta}), "linked": len(links), "cases": len(used), "path": path}


class Links:
    """화면용: 조항 group -> 비슷한 시정 사례 목록. 파일이 없으면 비어 있다."""

    def __init__(self, path: Path):
        data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
        self.links, self.cases = data.get("links", {}), data.get("cases", {})

    def __bool__(self):
        return bool(self.links)

    def similar(self, group: str) -> list[dict]:
        out = []
        for cid, sim in self.links.get(group, []):
            c = self.cases.get(cid)
            if c:
                out.append({"similarity": sim, "date": c["date"], "release": c["release"], "target": c["target"],
                            "issue": c["issue"], "action": c["action"], "law": c["law"], "url": c["url"],
                            "before": c["before"][:600], "after": c["after"][:600]})
        return out
