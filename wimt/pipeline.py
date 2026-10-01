"""수집 → 변경 판정 파이프라인 (제안서 3단계).

1 본문 추출        저장소의 각 버전 본문 (수집 단계에서 원문 그대로 저장됨)
2 정규화           normalize.py
3 해시 비교        같으면 해당 버전은 조항 비교 없이 종료
4 조항 단위 diff   diff.py (커밋 이력 = 변경 이력)
5 유불리 지수      score.py, 항 단위로 채점. 최초 버전은 전체, 이후는 바뀐 조항만 (캐시).
                   조 단위 전략에서 조의 지수는 가장 불리한 항의 지수.
6 불리해진 조항    조 안에서 바뀐 항을 다시 대응시켜, 불리한 항 추가·항 지수 하락·불리한 항 삭제를 판정
7 요약·알림        시행일과 행동 기한은 코드로 계산. 쉬운 말 요약(LLM)은 연결 지점만 두었다 (summary=None).
"""
import datetime as dt
from dataclasses import dataclass, field

from . import repo as R
from .chunk import Clause, chunk
from .diff import diff_clauses
from .normalize import content_hash, strip_marker
from .score import Item, Score

UNFAVORABLE = -0.7      # 지수가 이 이하면 '불리한 조항' (Jev 지수가 ±1.3 안에 모여 −1 에서 조정)
DROP = 0.5              # 이전 버전보다 이만큼 이상 떨어지면 '불리해진 조항' (Jev 지수 범위가 좁아 1 에서 조정)


@dataclass
class ScanResult:
    card: dict
    records: list[dict] = field(default_factory=list)
    current: list[dict] = field(default_factory=list)   # 최신 버전의 전체 조항과 지수
    skipped_same_hash: int = 0


def _date(s: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _version_layer(v: R.Version) -> dict:
    effective = _date(v.meta.get("시행일자")) or _date(v.meta.get("버전일자"))
    return {
        "commit": v.commit,
        "version_date": v.date.isoformat(),
        "fetched_at": v.meta.get("수집일자"),
        "effective_date": effective.isoformat() if effective else None,
        "notice_url": None,   # 개정 공지 수집은 아직 없음
    }


def _action_deadline(version_date: dt.date, effective: str | None) -> str | None:
    """시행 전에 알릴 수 있으면, 시행 전날을 이의 제기·해지 결정 기한으로 본다."""
    eff = _date(effective)
    if eff and eff > version_date:
        return (eff - dt.timedelta(days=1)).isoformat()
    return None


def _unit_changes(change_type: str, old: Clause | None, new: Clause | None,
                  old_scores: list | None, new_scores: list | None,
                  prev_keys: set | None = None, cur_keys: set | None = None) -> list[dict]:
    """조 안에서 바뀐 항과, 그 항이 '불리해짐'(became)·'불리한 항 삭제'(removed) 판정을 일으켰는지.

    항 단위 전략에서는 조항 하나 = 항 하나라서 조항의 변경이 그대로 한 줄이 된다.
    prev_keys·cur_keys: 이전·새 버전 전체의 항 비교 키. 조가 통째로 바뀌어(조 번호 변경, 큰 개정) 추가·삭제로 잡혀도
    같은 항이 다른 버전 어딘가에 그대로 있으면 추가·삭제로 보지 않는다 (옮겨진 항).
    """
    if change_type == "added":
        pairs = [("added", None, u, None, s) for u, s in zip(new.units, new_scores)]
    elif change_type == "removed":
        pairs = [("removed", u, None, s, None) for u, s in zip(old.units, old_scores)]
    elif len(old.units) == 1 and len(new.units) == 1:
        pairs = [("modified", old.units[0], new.units[0], old_scores[0], new_scores[0])]
    else:
        os_ = {id(u): s for u, s in zip(old.units, old_scores)}
        ns_ = {id(u): s for u, s in zip(new.units, new_scores)}
        pairs = [(ch.change_type, ch.old, ch.new, os_.get(id(ch.old)), ns_.get(id(ch.new)))
                 for ch in diff_clauses(old.units, new.units) if ch.change_type != "unchanged"]
    out = []
    for kind, o, n, os, ns in pairs:
        if kind == "added" and prev_keys and strip_marker(n.text) in prev_keys:
            continue
        if kind == "removed" and cur_keys and strip_marker(o.text) in cur_keys:
            continue
        delta = round(ns.favor_score - os.favor_score, 3) if (os and ns) else None
        flag = None
        if kind == "added" and ns.favor_score <= UNFAVORABLE:
            flag = "became"                                   # 불리한 항이 새로 추가됨
        elif kind == "modified" and delta is not None and delta <= -DROP:
            flag = "became"                                   # 같은 항의 지수가 크게 떨어짐
        elif kind == "removed" and os is not None and os.favor_score <= UNFAVORABLE:
            flag = "removed"                                  # 불리한 항이 삭제됨
        out.append({"clause_id": (n or o).clause_id, "change_type": kind,
                    "favor_score": ns.favor_score if ns else None, "old_score": os.favor_score if os else None,
                    "score_delta": delta, "flag": flag, "text": (n or o).text,
                    "old_text": o.text if kind == "modified" else None})
    return out


def _worst(scores: list[Score]) -> Score:
    """조 점수 = 가장 불리한 항의 점수 (항 평균을 내면 면책 항 하나가 긴 조에 묻힌다)."""
    return min(scores, key=lambda s: s.favor_score)


def _record(card, version_layer, clause: Clause, change_type, scores, old_scores, action_deadline,
            old_clause: Clause | None = None, prev_keys: set | None = None, cur_keys: set | None = None) -> dict:
    score = _worst(scores) if scores else None
    old_score = _worst(old_scores) if old_scores else None
    delta = round(score.favor_score - old_score.favor_score, 3) if (score and old_score) else None
    units = [] if change_type == "initial" else _unit_changes(change_type, old_clause, clause if scores else None,
                                                              old_scores, scores, prev_keys, cur_keys)
    return {
        # 문서
        "service": card["service"], "domain": card["domain"], "doc_type": card["doc_type"],
        "doc_title": card["title"], "path": card["path"], "source_url": card["source_url"],
        "fetch_method": card["fetch_method"], "is_public": card["is_public"],
        # 버전
        **version_layer,
        # 조항
        "clause_id": clause.clause_id, "article": clause.article, "title": clause.title, "text": clause.text,
        "clause_hash": content_hash(clause.text),
        # 이벤트 (대회 규정 등. 아직 수록 문서 없음)
        "event_id": None, "period": None, "status": None, "parent_doc": None, "parent_commit": None,
        # 유불리
        "favor_score": score.favor_score if score else None,
        "favor_confidence": score.favor_confidence if score else None,
        "scorer": score.scorer if score else None,
        "score_delta": delta,
        # 창으로 나눠 채점한 항: 항 id -> [{part, of, start, end, favor_score}] (위치는 그 항 원문 기준)
        "unit_windows": {u.clause_id: s.windows for u, s in zip(clause.units, scores or []) if s.windows} or None,
        "change_type": change_type,
        "is_unfavorable": bool(score and score.favor_score <= UNFAVORABLE),
        "became_unfavorable": any(u["flag"] == "became" for u in units),
        "removed_unfavorable": any(u["flag"] == "removed" for u in units),
        "unit_changes": units,
        "action_deadline": action_deadline,
        "summary": None,
    }


def scan(path: str, scorer, strategy: str = "article", repo=R.DEFAULT_REPO) -> ScanResult:
    versions = R.history(path, repo)
    card = R.data_card(path, repo, versions)
    res = ScanResult(card)
    prev_clauses: list[Clause] | None = None
    prev_scores: dict[int, list[Score]] = {}     # id(clause) -> 항별 Score
    lineage: dict[int, str] = {}                 # id(clause) -> 계보 id (처음 나타난 식별자@날짜)
    prev_hash = None

    def score_clauses(cs: list[Clause]) -> list[list[Score]]:
        """채점은 항 단위 (조 단위 전략이어도 항별 캐시를 그대로 쓴다)."""
        units = [u for c in cs for u in c.units]
        flat = iter(scorer.score([Item(card["service"], card["doc_type"], u.title, u.text) for u in units]))
        return [[next(flat) for _ in c.units] for c in cs]

    for v in versions:
        h = content_hash(v.body)
        if h == prev_hash:                          # 3 해시가 같으면 종료
            res.skipped_same_hash += 1
            continue
        clauses = chunk(v.body, strategy)
        vl = _version_layer(v)
        deadline = _action_deadline(v.date, vl["effective_date"])
        if prev_clauses is None:                    # 최초 수집: 전체 채점
            scores = score_clauses(clauses)
            for c, s in zip(clauses, scores):
                res.records.append(_record(card, vl, c, "initial", s, None, None))
                res.records[-1]["lineage"] = lineage[id(c)] = f"{c.clause_id}@{vl['version_date']}"
            cur_scores = {id(c): s for c, s in zip(clauses, scores)}
        else:
            changes = diff_clauses(prev_clauses, clauses)
            keys = ({strip_marker(u.text) for c in prev_clauses for u in c.units},
                    {strip_marker(u.text) for c in clauses for u in c.units})
            to_score = [ch.new for ch in changes if ch.change_type in ("added", "modified")]
            fresh = dict(zip(map(id, to_score), score_clauses(to_score)))
            cur_scores = {}
            for ch in changes:
                # 계보: 수정·동일은 이전 조항의 계보를 잇고, 추가는 새 계보. 조 번호가 다시 쓰여도 다른 조항과 섞이지 않는다.
                lin = lineage[id(ch.old)] if ch.old else f"{ch.new.clause_id}@{vl['version_date']}"
                if ch.new:
                    lineage[id(ch.new)] = lin
                if ch.change_type == "unchanged":
                    cur_scores[id(ch.new)] = prev_scores[id(ch.old)]
                    continue
                old_s = prev_scores.get(id(ch.old)) if ch.old else None
                if ch.change_type == "removed":
                    res.records.append(_record(card, vl, ch.old, "removed", None, old_s, deadline, ch.old, *keys))
                    res.records[-1]["lineage"] = lin
                    continue
                s = fresh[id(ch.new)]
                cur_scores[id(ch.new)] = s
                res.records.append(_record(card, vl, ch.new, ch.change_type, s, old_s, deadline, ch.old, *keys))
                res.records[-1]["lineage"] = lin
                if ch.change_type == "modified":
                    res.records[-1]["old_text"] = ch.old.text
                    res.records[-1]["similarity"] = round(ch.similarity, 3)
        prev_clauses, prev_scores, prev_hash = clauses, cur_scores, h

    # 최신 버전의 전체 조항 상태 ('지금 불리한 조항' 검색용). 조 단위면 항별 지수도 남긴다 (라벨링은 항 단위).
    res.current = []
    for c in prev_clauses or []:
        ss = prev_scores[id(c)]
        worst = _worst(ss)
        res.current.append({
            "service": card["service"], "doc_type": card["doc_type"], "path": card["path"],
            "doc_title": card["title"], "version_date": versions[-1].date.isoformat(),
            "clause_id": c.clause_id, "title": c.title, "text": c.text, "lineage": lineage[id(c)],
            "favor_score": worst.favor_score, "favor_confidence": worst.favor_confidence, "scorer": worst.scorer,
            "parts": [{"clause_id": u.clause_id, "text": u.text, "favor_score": s.favor_score,
                       "favor_confidence": s.favor_confidence, **({"windows": s.windows} if s.windows else {})}
                      for u, s in zip(c.parts, ss)],
            # 항이 없는 조항(조항 자체가 채점 단위)을 창으로 나눠 채점했으면 여기에 (위치는 text 기준)
            **({"windows": ss[0].windows} if not c.parts and ss[0].windows else {}),
        })
    return res
