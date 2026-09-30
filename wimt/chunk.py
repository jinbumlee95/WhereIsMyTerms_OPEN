"""청킹: 문서 본문(Markdown)을 조항 단위로 나눈다. 전략 세 가지를 비교한다.

- article (기본): 조 단위. `제N조`(또는 `N.` 번호 절) 하나가 한 단위. 그 안의 ①②… 항은 `parts` 로 들고 있어서,
  채점과 "어느 항이 바뀌었나"는 항 단위로 본다 (pipeline.py).
- paragraph: 항 단위. 조를 ①②… 항으로 나눈다. 항이 없으면 조 전체가 한 단위.
- heading: 제목 단위. Markdown 제목(#~######) 하나부터 다음 제목 전까지가 한 단위.

텍스트는 원문 그대로 둔다 (정규화는 비교할 때만).
"""
import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# ### 제38조 (회사의 면책) / ### 제1조. 목적 / ### 제 3 조의2 [정의]
ARTICLE = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*[.:]?\s*[(（\[【<〔]?\s*(.*?)\s*[)）\]】>〕]?\s*$")
# ### 1. 기본 운영 정책
SECTION = re.compile(r"^(\d{1,2})\.\s+(.+)$")
PARAGRAPH = re.compile(r"^\s*([①-⑳]|\[\d{1,2}\])")      # ① 또는 [1] (PUBG 등)


@dataclass
class Clause:
    clause_id: str      # 제38조⑦, 제38조, 1., 부칙, 전문 …
    article: str        # 조 식별자 (제38조)
    title: str          # 조 제목 (회사의 면책)
    text: str           # 원문
    order: int = 0
    parts: list["Clause"] = field(default_factory=list)    # 조 단위일 때 그 조의 항들

    @property
    def units(self) -> list["Clause"]:
        """채점 단위: 항이 있으면 항들, 없으면 자기 자신."""
        return self.parts or [self]


def _paragraphs(lines: list[str]) -> list[str]:
    paras, cur = [], []
    for line in lines:
        if line.strip():
            cur.append(line)
        elif cur:
            paras.append("\n".join(cur)); cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def _sections(body: str) -> list[tuple[int, str, list[str]]]:
    """(제목 수준, 제목, 본문 줄들). 첫 제목 전은 수준 0, 제목 '전문'."""
    out, level, title, buf = [], 0, "전문", []
    for line in body.replace("\r\n", "\n").split("\n"):
        m = HEADING.match(line)
        if m:
            out.append((level, title, buf))
            level, title, buf = len(m.group(1)), m.group(2).strip(), []
        else:
            buf.append(line)
    out.append((level, title, buf))
    return out


def article_id(heading: str) -> tuple[str, str] | None:
    """제목이 조(또는 번호 절)이면 (식별자, 조 제목)."""
    m = ARTICLE.match(heading)
    if m:
        return (f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")), m.group(3)
    m = SECTION.match(heading)
    if m:
        return f"{m.group(1)}.", m.group(2).strip()
    if re.fullmatch(r"부\s*칙", heading):
        return "부칙", "부칙"
    return None


def chunk_by_paragraph(body: str) -> list[Clause]:
    clauses: list[Clause] = []
    art, title = "전문", ""
    for level, heading, lines in _sections(body):
        # 조가 아닌 제목(문서 제목, 장 제목)은 조 문맥을 바꾸지 않는다
        found = article_id(heading) if level else None
        if found:
            art, title = found
        paras = _paragraphs(lines)
        if not paras:
            continue
        # 항(①…)으로 나눈다. 첫 항 앞 문단은 조 본문, 항 뒤의 번호 목록은 그 항에 붙는다.
        groups: list[tuple[str, list[str]]] = []
        for p in paras:
            m = PARAGRAPH.match(p)
            if m:
                groups.append((art + m.group(1), [p]))
            elif groups:
                groups[-1][1].append(p)
            else:
                groups.append((art, [p]))
        for cid, ps in groups:
            if clauses and clauses[-1].clause_id == cid:
                clauses[-1].text += "\n\n" + "\n\n".join(ps)   # 같은 조가 장 제목 등으로 끊긴 경우 이어 붙임
            else:
                clauses.append(Clause(cid, art, title, "\n\n".join(ps)))
    for i, c in enumerate(clauses):
        c.order = i
    return _dedupe_ids(clauses)


def chunk_by_article(body: str) -> list[Clause]:
    """항 단위 결과를 연속된 같은 조끼리 묶는다. 항이 하나뿐인 조는 parts 를 두지 않는다."""
    groups: list[list[Clause]] = []
    for c in chunk_by_paragraph(body):
        # 번호 없는 조 본문(제1조, 부칙, 부칙#2 …)은 새 조의 시작, 번호 있는 항(①, [1])은 앞 조에 붙는다
        is_body = c.clause_id == c.article or c.clause_id.startswith(c.article + "#")
        if groups and groups[-1][0].article == c.article and not is_body:
            groups[-1].append(c)
        else:
            groups.append([c])
    clauses = [Clause(g[0].article, g[0].article, g[0].title, "\n\n".join(c.text for c in g), i, g if len(g) > 1 else [])
               for i, g in enumerate(groups)]
    return _dedupe_ids(clauses)


def chunk_by_heading(body: str) -> list[Clause]:
    clauses = []
    for level, heading, lines in _sections(body):
        paras = _paragraphs(lines)
        if not paras:
            continue
        found = article_id(heading) if level else None
        art = found[0] if found else heading
        clauses.append(Clause(heading if level else "전문", art, heading, "\n\n".join(paras), len(clauses)))
    return _dedupe_ids(clauses)


def _dedupe_ids(clauses: list[Clause]) -> list[Clause]:
    """같은 식별자가 다시 나오면 #2, #3 을 붙인다 (예: 목차와 본문, 부칙 여러 개)."""
    seen: dict[str, int] = {}
    for c in clauses:
        n = seen.get(c.clause_id, 0) + 1
        seen[c.clause_id] = n
        if n > 1:
            c.clause_id = f"{c.clause_id}#{n}"
    return clauses


STRATEGIES = {"article": chunk_by_article, "paragraph": chunk_by_paragraph, "heading": chunk_by_heading}


def chunk(body: str, strategy: str = "article") -> list[Clause]:
    return STRATEGIES[strategy](body)
