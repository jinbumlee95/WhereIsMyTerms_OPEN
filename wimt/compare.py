"""변경 기록 비교 보기: 한 변경의 바뀌기 전·후, 그리고 그 변경 뒤 조항과 마지막 수집본 조항의 차이 (화면 하이라이트용).

diff 는 [종류, 내용] 목록이다. 종류: "eq"(같음), "del"(빠짐), "ins"(들어감), "skip"(같은 부분 접음, 내용은 글자 수).
줄 단위로 먼저 맞추고, 바뀐 줄 묶음 안에서만 낱말(한글 덩어리·영문·숫자·기호) 단위로 다시 비교한다.
긴 목록·표 조항(수만 자)도 줄 단위가 먼저라 빠르다. 비교는 화면 표시용이며 판정·답변에는 쓰지 않는다.
"""
import difflib
import re

CONTEXT = 120          # 바뀐 곳 앞뒤로 남기는 같은 글자 수
FOLD_MIN = 400         # 같은 구간이 이보다 길면 가운데를 접는다
WORD_DIFF_MAX = 20_000 # 바뀐 줄 묶음이 이보다 길면 낱말 단위 비교 없이 통째로 빠짐·들어감
TOKEN = re.compile(r"\s+|[가-힣]+|[A-Za-z]+|\d+|.", re.S)


def _push(ops: list, kind: str, text: str):
    if not text:
        return
    if ops and ops[-1][0] == kind:
        ops[-1][1] += text
    else:
        ops.append([kind, text])


def _words(a: str, b: str, ops: list):
    ta, tb = TOKEN.findall(a), TOKEN.findall(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            _push(ops, "eq", "".join(ta[i1:i2]))
        else:
            _push(ops, "del", "".join(ta[i1:i2]))
            _push(ops, "ins", "".join(tb[j1:j2]))


def diff(a: str, b: str) -> list[list]:
    """a(전) -> b(후) 의 차이. 같은 부분은 접지 않은 원래 목록."""
    la, lb = (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True)
    ops: list = []
    sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old, new = "".join(la[i1:i2]), "".join(lb[j1:j2])
        if tag == "equal":
            _push(ops, "eq", old)
        elif tag == "replace" and len(old) + len(new) <= WORD_DIFF_MAX:
            _words(old, new, ops)
        else:
            _push(ops, "del", old)
            _push(ops, "ins", new)
    return ops


def fold(ops: list[list], context: int = CONTEXT, fold_min: int = FOLD_MIN) -> list[list]:
    """바뀐 곳과 먼 같은 부분을 ["skip", 글자 수] 로 접는다 (처음·끝 구간은 바뀐 쪽만 남긴다)."""
    out = []
    for n, (kind, text) in enumerate(ops):
        if kind != "eq" or len(text) < fold_min:
            out.append([kind, text])
            continue
        head = context if n > 0 else 0                       # 앞에 바뀐 곳이 있을 때만 그 뒤를 남긴다
        tail = context if n < len(ops) - 1 else 0
        if len(text) - head - tail < fold_min // 2:
            out.append([kind, text])
            continue
        if head:
            out.append(["eq", text[:head]])
        out.append(["skip", len(text) - head - tail])
        if tail:
            out.append(["eq", text[-tail:]])
    return out


VIEW_MAX = 30_000      # 화면에 보낼 비교 하나의 최대 글자 수 (긴 목록 조항은 원문 확인으로)


def clip_ops(ops: list[list], limit: int = VIEW_MAX) -> list[list]:
    """글자 수 한도까지만. 넘으면 ["cut", 남은 글자 수] 로 끝낸다."""
    out, used = [], 0
    for n, (kind, text) in enumerate(ops):
        size = 0 if kind == "skip" else len(text)
        if used + size > limit:
            room = max(limit - used, 0)
            if room:
                out.append([kind, text[:room]])
            rest = size - room + sum(len(t) for k, t in ops[n + 1:] if k != "skip")
            out.append(["cut", rest])
            return out
        out.append([kind, text])
        used += size
    return out


def view(a: str, b: str) -> list[list]:
    return clip_ops(fold(diff(a, b)))


def changed(ops: list[list]) -> bool:
    return any(k in ("del", "ins") for k, _ in ops)


def change_view(db, change_id: str) -> dict | None:
    """변경 하나의 비교 보기. 없는 id 면 None.
    change: 이 변경의 전 -> 후 (추가면 전부 들어감, 삭제면 전부 빠짐).
    latest: 이 변경 뒤 조항 -> 마지막 수집본의 같은 계보 조항 (삭제됐거나 계보가 끊겼으면 latest 없음)."""
    row = db.change(change_id)
    if not row:
        return None
    kind, text = row["change_type"], row["text"] or ""
    # 추가·삭제 기록은 그 조항 본문을 text 에 둔다 (old_text 없음)
    before, after = {"added": ("", text), "removed": (text, "")}.get(kind, (row["old_text"] or "", text))
    out = {"id": row["id"], "path": row["path"], "clause_id": row["clause_id"], "title": row["title"] or "",
           "version_date": row["version_date"], "change_type": kind, "change": view(before, after)}
    cur = None if kind == "removed" else db.latest_clause(row["path"], row["lineage"])
    if cur:
        ops = diff(after, cur["text"] or "")
        out["latest"] = {"clause_id": cur["clause_id"], "version_date": cur["version_date"], "same": not changed(ops),
                         "diff": clip_ops(fold(ops))}
    return out
