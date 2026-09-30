"""조항 단위 diff: 이전 버전과 새 버전의 조항을 대응시켜 추가·삭제·수정·동일로 분류한다.

항 번호가 밀려도(⑦ 삽입 → 기존 ⑦이 ⑧) 같은 항으로 대응되도록, 번호를 뗀 정규화 텍스트로 정렬한다.
"""
import difflib
from dataclasses import dataclass

from .chunk import Clause
from .normalize import strip_marker

SIMILAR = 0.6          # 이 이상 비슷하면 '수정', 아니면 삭제 + 추가
MAX_PAIRING = 250      # 대응 후보가 이보다 크면 식별자로만 대응 (계산량 제한)


@dataclass
class Change:
    change_type: str            # added | removed | modified | unchanged
    old: Clause | None
    new: Clause | None
    similarity: float = 1.0


LONG = 3000            # 이보다 긴 조항(큰 표 등)은 줄 단위로 비교 (글자 단위는 너무 느림)


def _ratio(a: str, b: str) -> float:
    if max(len(a), len(b)) > LONG:
        a, b = a.split("\n"), b.split("\n")
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() < SIMILAR or m.quick_ratio() < SIMILAR:
        return 0.0
    return m.ratio()


def _pair_block(old: list[Clause], new: list[Clause], ok: list[str], nk: list[str]) -> list[Change]:
    """교체 구간 안에서 순서를 지키며 가장 비슷한 조항끼리 짝짓는다."""
    if len(old) * len(new) > MAX_PAIRING * MAX_PAIRING:
        by_id = {c.clause_id: i for i, c in enumerate(new)}
        pairs = {i: by_id[c.clause_id] for i, c in enumerate(old) if c.clause_id in by_id}
    else:
        pairs, last = {}, -1
        for i in range(len(old)):
            best, best_j = 0.0, None
            for j in range(last + 1, len(new)):
                r = _ratio(ok[i], nk[j])
                if r > best:
                    best, best_j = r, j
            if best_j is not None and best >= SIMILAR:
                pairs[i] = best_j
                last = best_j
    out, used = [], set(pairs.values())
    rev = {j: i for i, j in pairs.items()}
    oi = 0
    for j in range(len(new)):
        if j in rev:
            i = rev[j]
            while oi < i:                      # 짝이 없는 이전 조항은 이 자리에서 삭제
                if oi not in pairs:
                    out.append(Change("removed", old[oi], None, 0.0))
                oi += 1
            sim = _ratio(ok[i], nk[j]) if ok[i] != nk[j] else 1.0
            out.append(Change("unchanged" if ok[i] == nk[j] else "modified", old[i], new[j], sim))
            oi = i + 1
        elif j not in used:
            out.append(Change("added", None, new[j], 0.0))
    for i in range(oi, len(old)):
        if i not in pairs:
            out.append(Change("removed", old[i], None, 0.0))
    return out


def _pair_moved(changes: list[Change]) -> list[Change]:
    """순서가 바뀐 조항 짝짓기: 남은 삭제·추가 중 비슷한 것끼리 순서와 상관없이 '수정'(내용이 같으면 '동일')으로 묶는다.

    개정 때 조항 순서를 바꾸면(쿠팡 2013-08-30: 제16조 → 제7조) 앞의 정렬은 순서를 지키는 짝만 찾으므로
    옮겨진 조항이 삭제 + 추가로 남는다. 그러면 계보가 끊기고 '삭제된 조항'이 잘못 기록된다."""
    rem = [i for i, c in enumerate(changes) if c.change_type == "removed"]
    add = [j for j, c in enumerate(changes) if c.change_type == "added"]
    if not rem or not add or len(rem) * len(add) > MAX_PAIRING * MAX_PAIRING:
        return changes
    key = {i: strip_marker(changes[i].old.text) for i in rem} | {j: strip_marker(changes[j].new.text) for j in add}
    cands = sorted(((r, i, j) for i in rem for j in add if (r := _ratio(key[i], key[j])) >= SIMILAR), reverse=True)
    drop, used = set(), set()
    for r, i, j in cands:                      # 가장 비슷한 쌍부터
        if i in drop or j in used:
            continue
        same = key[i] == key[j]
        changes[j] = Change("unchanged" if same else "modified", changes[i].old, changes[j].new, 1.0 if same else r)
        drop.add(i); used.add(j)
    # 제목이 같은 조: 번호를 옮기며 내용을 다시 쓴 조(티빙 유료이용약관 2026-08-03 제26조 → 제21조 '분쟁의 해결')는
    # 유사도가 낮아도 같은 조항이다. 조 전체 단위이고 그 버전에서 제목이 삭제·추가 한 쌍뿐일 때만 짝짓는다
    # (항 단위면 한 조의 항들이 제목을 같이 쓴다).
    def by_title(idx, side):
        out = {}
        for n in idx:
            c = getattr(changes[n], side)
            if n not in drop | used and c.title and c.clause_id == c.article:
                out.setdefault(c.title, []).append(n)
        return {t: ns[0] for t, ns in out.items() if len(ns) == 1}
    old_t, new_t = by_title(rem, "old"), by_title(add, "new")
    for t in old_t.keys() & new_t.keys():
        i, j = old_t[t], new_t[t]
        changes[j] = Change("modified", changes[i].old, changes[j].new, _ratio(key[i], key[j]))
        drop.add(i)
    return [c for n, c in enumerate(changes) if n not in drop]


def diff_clauses(old: list[Clause], new: list[Clause]) -> list[Change]:
    ok = [strip_marker(c.text) for c in old]
    nk = [strip_marker(c.text) for c in new]
    changes: list[Change] = []
    sm = difflib.SequenceMatcher(None, ok, nk, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            changes += [Change("unchanged", old[i1 + k], new[j1 + k]) for k in range(i2 - i1)]
        elif tag == "delete":
            changes += [Change("removed", c, None, 0.0) for c in old[i1:i2]]
        elif tag == "insert":
            changes += [Change("added", None, c, 0.0) for c in new[j1:j2]]
        else:
            changes += _pair_block(old[i1:i2], new[j1:j2], ok[i1:i2], nk[j1:j2])
    return _pair_moved(changes)
