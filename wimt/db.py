"""변경 이력 DB (SQLite): scan 결과를 표로 저장해, 검색이 아니라 조회로 이력을 가져온다.

  documents     문서 (서비스·회사·문서 종류·제목·출처·버전 수·최초/최신 버전일)
  versions      문서 버전 (커밋·버전일·시행일·그 버전에서 바뀐 조항 수)
  changes       조항 변경 (추가·삭제·수정). id 는 index.change_id 와 같아 벡터 색인과 이어진다.
  unit_changes  조 안에서 바뀐 항 (전·후 텍스트, 항 지수)
  clauses       최신 버전 조항 (index.group_id 와 같은 id)

RAG 에서 쓰는 조회
  timeline(path, lineage)      조항 하나의 변경 이력 전체 (날짜순). 계보(lineage)로 묶어서, 조 번호가 다시 쓰인
                               다른 조항(예: 옛 제38조 '게시물의 관리' vs 지금 제38조 '회사의 면책')은 섞이지 않는다.
  doc_versions(path)           문서의 개정일 목록과 개정마다 바뀐 조항 수
  changes_between(...)         회사·문서·기간으로 변경 목록
"""
import json
import sqlite3
import threading
from pathlib import Path

from . import index as I

SCHEMA = """
CREATE TABLE documents (path TEXT PRIMARY KEY, service TEXT, company TEXT, domain TEXT, doc_type TEXT, title TEXT,
                        source_url TEXT, versions INTEGER, first_version TEXT, latest_version TEXT);
CREATE TABLE versions (path TEXT, "commit" TEXT, version_date TEXT, effective_date TEXT, fetched_at TEXT,
                       changed INTEGER, PRIMARY KEY (path, "commit"));
CREATE TABLE changes (id TEXT PRIMARY KEY, path TEXT, service TEXT, company TEXT, clause_id TEXT, article TEXT, lineage TEXT,
                      title TEXT, version_date TEXT, effective_date TEXT, "commit" TEXT, change_type TEXT,
                      text TEXT, old_text TEXT, similarity REAL, favor_score REAL, score_delta REAL, change_text TEXT);
CREATE TABLE unit_changes (change_id TEXT, clause_id TEXT, change_type TEXT, text TEXT, old_text TEXT,
                           favor_score REAL, old_score REAL, flag TEXT);
CREATE TABLE clauses (id TEXT PRIMARY KEY, path TEXT, service TEXT, company TEXT, clause_id TEXT, lineage TEXT, title TEXT,
                      text TEXT, version_date TEXT, favor_score REAL);
CREATE INDEX changes_article ON changes (path, article, version_date);
CREATE INDEX changes_lineage ON changes (path, lineage, version_date);
CREATE INDEX changes_date ON changes (company, version_date);
CREATE INDEX unit_changes_id ON unit_changes (change_id);
"""


def _article(clause_id: str) -> str:
    """제38조#2 -> 제38조 (목차·중복으로 붙은 번호를 떼어 같은 조의 이력으로 묶는다)."""
    return clause_id.split("#")[0]


def build(path: Path, records: list[dict], current: list[dict], cards: list[dict] | None = None) -> dict:
    """scan 레코드와 최신 조항으로 DB 를 새로 만든다 (항상 전체 재생성, 몇 초)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)

    versions: dict[tuple, dict] = {}
    docs: dict[str, dict] = {}
    for r in records:
        v = versions.setdefault((r["path"], r["commit"]), {
            "path": r["path"], "commit": r["commit"], "version_date": r["version_date"],
            "effective_date": r.get("effective_date"), "fetched_at": r.get("fetched_at"), "changed": 0})
        if r["change_type"] != "initial":
            v["changed"] += 1
        docs.setdefault(r["path"], {"path": r["path"], "service": r["service"], "company": r["service"].split("/")[0],
                                    "domain": r.get("domain"), "doc_type": r["doc_type"], "title": r.get("doc_title"),
                                    "source_url": r.get("source_url")})
    for c in cards or []:
        if c["path"] in docs:
            docs[c["path"]]["title"] = docs[c["path"]]["title"] or c.get("title")
    for p, d in docs.items():
        dates = sorted(v["version_date"] for (vp, _), v in versions.items() if vp == p)
        d.update(versions=len(dates), first_version=dates[0], latest_version=dates[-1])

    con.executemany("INSERT INTO documents VALUES (:path, :service, :company, :domain, :doc_type, :title, :source_url, "
                    ":versions, :first_version, :latest_version)", docs.values())
    con.executemany('INSERT INTO versions VALUES (:path, :commit, :version_date, :effective_date, :fetched_at, :changed)',
                    versions.values())

    rows, units, seen = [], [], {}
    for r in records:
        if r["change_type"] == "initial":
            continue
        cid = I.change_id(r)
        seen[cid] = seen.get(cid, 0) + 1
        if seen[cid] > 1:
            cid += f"#{seen[cid]}"                          # index.change_entries 와 같은 규칙
        rows.append((cid, r["path"], r["service"], r["service"].split("/")[0], r["clause_id"], _article(r["clause_id"]),
                     r.get("lineage"),
                     r.get("title"), r["version_date"], r.get("effective_date"), r["commit"], r["change_type"],
                     r["text"], r.get("old_text"), r.get("similarity"), r.get("favor_score"), r.get("score_delta"),
                     I.change_text(r)))
        units += [(cid, u["clause_id"], u["change_type"], u.get("text"), u.get("old_text"), u.get("favor_score"),
                   u.get("old_score"), u.get("flag")) for u in r.get("unit_changes") or []]
    con.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO unit_changes VALUES (?,?,?,?,?,?,?,?)", units)
    con.executemany("INSERT INTO clauses VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(I.group_id(c), c["path"], c["service"], c["service"].split("/")[0], c["clause_id"], c.get("lineage"),
                      c.get("title"),
                      c["text"], c["version_date"], c["favor_score"]) for c in current])
    con.commit()
    con.close()
    tmp.replace(path)
    return {"documents": len(docs), "versions": len(versions), "changes": len(rows), "unit_changes": len(units),
            "clauses": len(current)}


class DB:
    def __init__(self, path: Path):
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} 가 없습니다. 먼저 python -m wimt db 를 실행하세요.")
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        # 웹 서버·평가 도구의 작업 스레드 여럿이 연결 하나를 함께 쓴다. sqlite3 연결은 동시 실행을 막아 주지 않아
        # 2026-10-02 질문 4개를 동시에 돌리다 "InterfaceError: bad parameter or other API misuse"가 났다.
        self.lock = threading.Lock()

    def _all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args)]

    def _one(self, sql: str, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchone()

    def document(self, path: str) -> dict | None:
        rows = self._all("SELECT * FROM documents WHERE path = ?", (path,))
        return rows[0] if rows else None

    def lineage_of(self, path: str, clause_id: str) -> str | None:
        row = self._one("SELECT lineage FROM clauses WHERE path = ? AND clause_id = ?", (path, clause_id))
        return row[0] if row else None

    def timeline(self, path: str, clause_id: str, date_from: str | None = None, date_to: str | None = None,
                 lineage: str | None = None) -> list[dict]:
        """조항 하나의 변경 이력 (날짜순). 최신 조항이면 계보로, 아니면(이미 삭제된 조항 등) 조 번호로 찾는다.
        lineage 를 주면(변경 검색에 걸린 삭제 조항 등) 그 계보로. 기간을 주면 그 안의 변경만."""
        lin = lineage or self.lineage_of(path, clause_id)
        if lin:
            sql, args = "SELECT * FROM changes WHERE path = ? AND lineage = ?", [path, lin]
        else:
            sql, args = "SELECT * FROM changes WHERE path = ? AND article = ?", [path, _article(clause_id)]
        if date_from:
            sql += " AND version_date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND version_date <= ?"; args.append(date_to)
        return self._all(sql + " ORDER BY version_date, id", args)

    def doc_versions(self, path: str) -> list[dict]:
        return self._all("SELECT version_date, effective_date, changed FROM versions WHERE path = ? ORDER BY version_date",
                         (path,))

    def documents(self) -> list[dict]:
        return self._all("SELECT * FROM documents ORDER BY path")

    def changes_between(self, company: str | None = None, path: str | None = None, date_from: str | None = None,
                        date_to: str | None = None, limit: int = 50, month_day: str | None = None) -> list[dict]:
        """회사·문서·기간으로 변경 목록 (최신순). month_day("07-25" 또는 "07")는 연도와 상관없이 그 월(일)."""
        sql, args = "SELECT * FROM changes WHERE 1=1", []
        for col, val, op in (("company", company, "="), ("path", path, "="), ("version_date", date_from, ">="),
                             ("version_date", date_to, "<="), ("substr(version_date, 6)", month_day and month_day + "%", "LIKE")):
            if val:
                sql += f" AND {col} {op} ?"; args.append(val)
        return self._all(sql + " ORDER BY version_date DESC, id LIMIT ?", args + [limit])

    def stats(self) -> dict:
        return {t: self._one(f"SELECT COUNT(*) FROM {t}")[0]
                for t in ("documents", "versions", "changes", "unit_changes", "clauses")}


def dumps(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2)
