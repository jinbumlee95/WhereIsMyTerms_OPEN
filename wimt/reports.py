"""산출물: 조항 통계표 · 변경 수동 검사표 · 유불리 라벨링 비교표."""
import csv
import random
import statistics
from pathlib import Path

from .chunk import STRATEGIES
from .pipeline import UNFAVORABLE as UNFAVORABLE_LABEL
from .score import Item


def write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:   # 엑셀에서 한글이 깨지지 않게 BOM
        fields = list(dict.fromkeys(k for r in rows for k in r)) or ["empty"]    # 행마다 열이 다를 수 있다
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def read_csv(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 조항 통계표: 청킹 전략 비교 (조항 번호 단위 vs 제목 단위)
# ---------------------------------------------------------------------------
def clause_stats(docs: list[tuple[str, str]]) -> list[dict]:
    """docs: [(경로, 최신 본문)]"""
    rows = []
    for path, body in docs:
        row = {"doc": path, "chars": len(body)}
        for name, fn in STRATEGIES.items():
            units = fn(body)
            lens = [len(u.text) for u in units] or [0]
            row[f"{name}_units"] = len(units)
            row[f"{name}_median_chars"] = int(statistics.median(lens))
            row[f"{name}_max_chars"] = max(lens)
            row[f"{name}_over_2000"] = sum(1 for n in lens if n > 2000)   # 한 번에 채점하기 긴 단위
        row["article_numbered"] = sum(1 for u in STRATEGIES["article"](body) if u.article.startswith("제"))
        rows.append(row)
    return rows


def summarize_stats(rows: list[dict]) -> str:
    def tot(k):
        return sum(r[k] for r in rows)
    lines = [f"문서 {len(rows)}개"]
    for name in STRATEGIES:
        meds = [r[f"{name}_median_chars"] for r in rows]
        lines.append(f"- {name:8}: 단위 {tot(name + '_units'):>6}개, 문서별 중앙 길이의 중앙값 {int(statistics.median(meds))}자, "
                     f"2000자 넘는 단위 {tot(name + '_over_2000')}개")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 변경 수동 검사표: 감지된 변경 20~30건을 직접 열어 가짜 변경 비율을 본다
# ---------------------------------------------------------------------------
def change_review(records: list[dict], n: int = 25, seed: int = 7) -> list[dict]:
    changes = [r for r in records if r["change_type"] in ("added", "removed", "modified")]
    rng = random.Random(seed)
    # 문서가 한쪽에 몰리지 않게 문서별로 섞어 고른다
    by_doc: dict[str, list[dict]] = {}
    for r in changes:
        by_doc.setdefault(r["path"], []).append(r)
    pools = [rng.sample(v, len(v)) for v in by_doc.values()]
    rng.shuffle(pools)
    picked = []
    while len(picked) < n and any(pools):
        for p in pools:
            if p and len(picked) < n:
                picked.append(p.pop())
    return [_review_row(r) for r in picked]


def _review_row(r: dict, extra: dict | None = None) -> dict:
    return {
        "doc": r["path"], "version_date": r["version_date"], "commit": r["commit"][:10],
        "change_type": r["change_type"], "clause_id": r["clause_id"],
        "similarity": r.get("similarity", ""), "favor_score": r["favor_score"], "score_delta": r["score_delta"],
        "changed_words": changed_words(r.get("old_text") or "", r["text"]) if r["change_type"] == "modified" else "",
        "old_text": (r.get("old_text") or (r["text"] if r["change_type"] == "removed" else ""))[:1500],
        "new_text": ("" if r["change_type"] == "removed" else r["text"])[:1500],
        **(extra or {"가짜변경(Y/N)": "", "메모": ""}),
    }


def changed_words(old: str, new: str, limit: int = 4) -> str:
    """바뀐 부분만 짧게: '전' → '후' (검사할 때 어디가 바뀌었는지 바로 보이게)."""
    import difflib
    a, b = old.split(), new.split()
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            parts.append(f"'{' '.join(a[i1:i2])[:60]}' → '{' '.join(b[j1:j2])[:60]}'")
    return "; ".join(parts[:limit]) + (f" 외 {len(parts) - limit}곳" if len(parts) > limit else "")


def drop_review(records: list[dict]) -> list[dict]:
    """항 지수 하락으로 '불리해진 조항'이 된 수정 전부. 실제 불리한 변경인지, 채점이 흔들린 것인지 가린다.

    조 단위면 원인이 된 항의 전·후 텍스트를 보여 준다.
    """
    rows = []
    for r in records:
        for u in r.get("unit_changes") or []:
            if u["flag"] == "became" and u["change_type"] == "modified":
                rows.append({
                    "doc": r["path"], "version_date": r["version_date"], "commit": r["commit"][:10],
                    "clause_id": u["clause_id"], "old_score": u["old_score"], "favor_score": u["favor_score"],
                    "score_delta": u["score_delta"], "changed_words": changed_words(u["old_text"], u["text"]),
                    "old_text": u["old_text"][:1500], "new_text": u["text"][:1500],
                    "판정(실제 불리/채점 흔들림/기타)": "", "메모": "",
                })
    return sorted(rows, key=lambda x: x["score_delta"])


# ---------------------------------------------------------------------------
# 유불리 라벨링 비교표: 조항 50개 내외를 직접 라벨링해 지수와 비교하고 기준값을 조정한다
# ---------------------------------------------------------------------------
def labeling_sheet(current: list[dict], n: int = 50, seed: int = 11) -> list[dict]:
    """current: 문서별 최신 조항 목록을 합친 것. 문서 종류별로 고르게 뽑는다. 모델 지수는 넣지 않는다 (편향 방지).

    채점 단위가 항이므로 조 단위 결과도 항으로 풀어서 뽑는다.
    """
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    units = [{**c, **p} for c in current for p in (c.get("parts") or [{}])]
    for c in units:
        if 40 <= len(c["text"]) <= 1500:
            by_type.setdefault(c["doc_type"], []).append(c)
    pools = [rng.sample(v, len(v)) for _, v in sorted(by_type.items())]
    picked = []
    while len(picked) < n and any(pools):
        for p in pools:
            if p and len(picked) < n:
                picked.append(p.pop())
    return [{
        "id": i + 1, "service": c["service"], "doc_type": c["doc_type"], "doc": c["path"],
        "clause_id": c["clause_id"], "title": c["title"], "text": c["text"],
        "사람라벨(-2~2)": "", "메모": "",
    } for i, c in enumerate(picked)]


def labeling_compare(rows: list[dict], scorer) -> tuple[list[dict], dict]:
    labeled = [r for r in rows if str(r.get("사람라벨(-2~2)", "")).strip() not in ("",)]
    if not labeled:
        return [], {"labeled": 0}
    scores = scorer.score([Item(r["service"], r["doc_type"], r["title"], r["text"]) for r in labeled])
    out = []
    for r, s in zip(labeled, scores):
        human = int(float(r["사람라벨(-2~2)"]))
        out.append({"id": r["id"], "doc": r["doc"], "clause_id": r["clause_id"], "human": human,
                    "favor_score": s.favor_score, "favor_confidence": s.favor_confidence,
                    "model_level": max(-2, min(2, round(s.favor_score))), "abs_error": round(abs(s.favor_score - human), 3)})
    # '불리한 조항' 기준값: 사람 라벨(정수)이 불리 이하(≤ -1)인 것을 정답으로 F1 이 가장 높은 지수 기준
    truth = [o["human"] <= UNFAVORABLE_LABEL for o in out]
    best = (None, -1.0)
    for k in range(-20, 1):
        th = k / 10
        pred = [o["favor_score"] <= th for o in out]
        tp = sum(p and t for p, t in zip(pred, truth))
        fp = sum(p and not t for p, t in zip(pred, truth))
        fn = sum(t and not p for p, t in zip(pred, truth))
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        if f1 > best[1]:
            best = (th, f1)
    summary = {
        "labeled": len(out),
        "mae": round(statistics.mean(o["abs_error"] for o in out), 3),
        "exact_level_agreement": round(sum(o["model_level"] == o["human"] for o in out) / len(out), 3),
        "within_one_level": round(sum(abs(o["model_level"] - o["human"]) <= 1 for o in out) / len(out), 3),
        "best_unfavorable_threshold": best[0], "best_f1": round(best[1], 3),
    }
    return out, summary
