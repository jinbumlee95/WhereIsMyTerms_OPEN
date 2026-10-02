"""질문 처리 흐름(flow)의 웹 화면: 회사 고르기 → 질문 → 단계별 진행(스트리밍) → 답변·인용·근거.

- 서버는 표준 라이브러리(ThreadingHTTPServer). 화면은 web_static/ 의 HTML·CSS·JS (빌드 없음).
- POST /api/ask/stream: LangGraph 를 stream_mode=["tasks", "values"] 로 돌려, 노드가 시작·끝날 때마다
  SSE 이벤트를 보낸다 (Jev 판정처럼 몇 초 걸리는 단계도 "진행 중"으로 보인다). 마지막에 답변·인용·근거.
  검색·근거 판정·법령 조회·추가 검색이 끝날 때마다 그때까지 찾은 근거 목록(live)도 보내, 화면이 근거를 바로 쌓아 보인다.
- 여러 사용자가 동시에 쓴다: 작업 스레드 WORKERS 개(WIMT_WEB_WORKERS, 기본 4)가 흐름 하나를 함께 쓰고,
  넘치는 질문은 QUEUE 개까지 기다리게 한 뒤(화면에 '앞에 n개' 상태), 그보다 많으면 503 으로 거절한다.
"""
import json
import logging
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import flow, rag
from . import services as S

STATIC = Path(__file__).with_name("web_static")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
PAGES = {"/": "index.html", "": "index.html", "/rules": "rules.html"}   # 주소 -> 화면 파일
STEP_NAMES = {"contextualize": "질문 이해", "classify": "분기 판정", "direct_answer": "즉시 답변", "clarify": "회사 되묻기", "retrieve": "검색", "grade": "근거 판정", "laws": "법령 조회",
              "check_context": "문맥 충분성 판정", "expand": "추가 검색", "answer": "답변", "abstain": "답변 보류"}


def companies(clauses: list[dict], sources: dict | None = None) -> list[dict]:
    """회사별 문서 목록. sources(경로 -> 이력 DB 문서)가 있으면 이력 DB 의 문서를 기준으로 삼고 원문 주소·최근 개정일도 붙인다.
    이력 DB 는 scan 결과로 다시 만들므로 색인과 같은 문서를 가리킨다. 없으면(테스트 등) 최신 조항에서 뽑는다."""
    sources = sources or {}
    titles = {c["path"]: c.get("doc_title") for c in clauses}
    docs: dict[str, dict[str, str]] = {}
    for p in sources or titles:
        docs.setdefault(p.split("/")[0], {})[p] = ((sources.get(p) or {}).get("title") or titles.get(p)
                                                  or Path(p).stem)
    out = []
    for k, v in sorted(docs.items()):
        items = [{"path": p, "title": t, "source_url": (sources.get(p) or {}).get("source_url") or "",
                  "latest_version": (sources.get(p) or {}).get("latest_version") or ""} for p, t in sorted(v.items())]
        aliases = sorted({a for svc, words in S.ALIASES.items() if svc.split("/")[0] == k for a in words})
        out.append({"id": k, "name": S.company_name(k), "documents": items, "aliases": aliases,   # 화면 검색용 (배그, 마모 …)
                    "latest_version": max((d["latest_version"] for d in items), default="")})
    return out


def evidence(state: dict, sources: dict | None = None, links=None) -> list[dict]:
    """답변에 쓴 근거 (답변 LLM 에 넘긴 것과 같은 번호 [C1] …) 를 화면에 보여 줄 수 있게.
    정본은 각 기업의 공식 페이지이므로 근거마다 원문 주소(source_url)를 붙인다.
    highlights: 답변이 이 근거를 인용한 문장과 가장 비슷한 근거 문장의 위치 [[시작, 끝], …] (화면이 표시한다).
    favor_score: Jev 유불리 지수 (Jev 가 매긴 것만). ftc: 문구가 비슷한 공정위 시정 사례 (links 가 있으면, 수집본 조항 C 만).
    둘 다 화면 표시용이고 답변 LLM 에는 넘기지 않는다."""
    if not state.get("res"):
        return []
    sources = sources or {}
    ctx, cites = rag.context_blocks(flow.keep_only(state["res"], set(state.get("relevant", []))))
    blocks = ctx.split("\n\n---\n\n") if ctx else []
    items = [{**c, "text": b.split("\n", 1)[-1] if "\n" in b else "", "head": b.split("\n", 1)[0],
              "source_url": c.get("source_url") or (sources.get(c["path"]) or {}).get("source_url") or ""}
             for c, b in zip(cites, blocks)]
    marks = highlights(state.get("answer") or "", {e["tag"]: e["text"] for e in items}) if state.get("citations") else {}
    for e in items:
        e["highlights"] = marks.get(e["tag"], [])
        if links and e["tag"][0] == "C":
            e["ftc"] = links.similar(f"{e['path']}::{e['clause_id']}")
    return items


# ---------------------------------------------------------------------------
# 근거에서 답변이 실제로 참고한 부분 (LLM 호출 없이 글자 비교로)
# ---------------------------------------------------------------------------
HL_MIN = 0.3          # 답변 문장과 근거 문장의 글자 2-gram 겹침(Dice) 이 이 이상이면 표시
HL_PER_SENTENCE = 2   # 답변 문장 하나가 근거 하나에서 표시할 최대 문장 수
TAG = re.compile(r"\[([CTHDL]\d+)\]")
SPLIT_ANSWER = re.compile(r"(?<=[.!?。])\s+|\n+")
SEGMENT = re.compile(r"[^\n]+?(?:[.!?。](?=\s|$)|$)", re.M)   # 근거 본문: 줄 안에서 문장 단위 (끝 문장부호 포함)


def _grams(text: str) -> set[str]:
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def highlights(answer: str, texts: dict[str, str]) -> dict[str, list[list[int]]]:
    """답변 문장마다 그 문장이 인용한 근거([C1] …)에서 글자 2-gram 이 가장 많이 겹치는 문장을 고른다.
    반환: 태그 -> 근거 본문 안의 [시작, 끝] 목록 (겹치는 구간은 합친다). 답변이 바꿔 쓴 문장은 못 찾을 수 있다."""
    segs = {tag: [(m.start(), m.end(), _grams(m.group(0))) for m in SEGMENT.finditer(text) if m.group(0).strip()]
            for tag, text in texts.items()}
    found: dict[str, list[list[int]]] = {}
    pairs = []                                         # (답변 문장, 그 문단의 인용 태그): 태그는 보통 문단 끝에만 붙는다
    for para in re.split(r"\n\s*\n", answer):
        tags = [t for t in dict.fromkeys(TAG.findall(para)) if t in segs]
        pairs += [(s, tags) for s in SPLIT_ANSWER.split(para) if s.strip()]
    for sentence, tags in pairs:
        a = _grams(TAG.sub("", sentence).replace("**", ""))
        if not tags or len(a) < 4:
            continue
        for tag in tags:
            scored = sorted(((2 * len(a & g) / (len(a) + len(g)), s, e) for s, e, g in segs[tag] if len(g) >= 4),
                            reverse=True)
            for score, s, e in scored[:HL_PER_SENTENCE]:
                if score >= HL_MIN:
                    found.setdefault(tag, []).append([s, e])
    out = {}
    for tag, ranges in found.items():                  # 겹치거나 이어진 구간 합치기
        merged = []
        for s, e in sorted(ranges):
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out[tag] = merged
    return out


def step_info(result: dict) -> dict:
    """노드가 끝났을 때 화면에 보여 줄 요약 (그 노드가 trace 에 남긴 마지막 항목)."""
    info = dict((result.get("trace") or [{}])[-1])
    info.pop("grades", None)                 # 근거별 확률은 길어서 최종 결과의 trace 로만
    return info


LIVE_STEPS = ("retrieve", "grade", "laws", "expand")   # 끝나면 지금까지 찾은 근거를 화면에 바로 보낸다
LIVE_SNIPPET = 140                                      # 진행 중 근거 카드에 보일 본문 앞부분 글자 수
LIVE_MAX = 40                                           # 한 번에 보낼 근거 수 (넘으면 관련도 높은 순으로 자른다)


def live_evidence(state: dict) -> list[dict]:
    """진행 중에 보여 줄 근거 목록: 찾은 근거마다 이름과 본문 앞부분, 판정을 마쳤으면 관련도와 채택 여부.
    연출용 미리보기이고 답변은 최종 결과의 근거(evidence)로만 만든다."""
    if not state.get("res"):
        return []
    grades, kept = state.get("grades") or {}, set(state.get("relevant") or [])
    out = []
    for e in flow.evidence(state["res"]):
        body = e["text"].split("\n", 1)[-1] if "\n" in e["text"] else e["text"]
        g = grades.get(e["id"])
        out.append({"id": e["id"], "kind": e["id"][0],
                    "label": " · ".join(x for x in (e["document"], e["clause"], e["title"]) if x),
                    "date": e.get("version_date") or "", "change_type": e.get("change_type") or "",
                    "snippet": re.sub(r"\s+", " ", body).strip()[:LIVE_SNIPPET],
                    "grade": round(g, 2) if g is not None else None,
                    "kept": (e["id"] in kept) if g is not None else None})
    if len(out) > LIVE_MAX:
        out = sorted(out, key=lambda x: -(x["grade"] or 0))[:LIVE_MAX]
    return out


WORKERS = int(os.environ.get("WIMT_WEB_WORKERS") or 4)   # 동시에 처리하는 질문 수 (여러 사용자)
QUEUE = WORKERS * 2                                        # 처리 중인 질문 외에 기다릴 수 있는 질문 수


class WebApp:
    def __init__(self, clauses: list[dict], make_app, documents: list[dict] | None = None,
                 workers: int = WORKERS, queue: int = QUEUE, links=None):
        """make_app() -> 컴파일된 흐름. 한 번만 만들어 모든 작업 스레드가 함께 쓴다 (검색기는 읽기 전용,
        SQLite 는 스레드 공유 연결, 로컬 임베딩 모델과 법령 캐시는 각자 잠금으로 보호한다).
        동시에 workers 개를 처리하고 queue 개까지 기다리게 하며, 그보다 많으면 거절한다.
        documents: 이력 DB 의 문서 목록 (원문 주소 source_url, 최근 개정일 latest_version).
        links: ftc.Links (조항 -> 문구가 비슷한 공정위 시정 사례). 시작할 때 한 번 읽고 바꾸지 않는다."""
        self.sources = {d["path"]: d for d in documents or []}
        self.links = links
        self.companies = companies(clauses, self.sources)
        self.allowed = {c["id"] for c in self.companies}
        self.make_app, self.app = make_app, None
        self.workers = workers
        self.worker = ThreadPoolExecutor(max_workers=workers)
        self.slots = threading.BoundedSemaphore(workers + queue)
        self.build_lock, self.count_lock = threading.Lock(), threading.Lock()
        self.inflight = 0                     # 받아서 아직 끝나지 않은 질문 (처리 중 + 대기)

    def flow_app(self):
        """흐름을 처음 한 번만 만든다 (여러 스레드가 동시에 불러도 한 번)."""
        with self.build_lock:
            if self.app is None:
                self.app = self.make_app()
            return self.app

    def warm(self):
        """서버를 띄우자마자 작업 스레드에서 흐름을 만든다 (검색기·임베딩 준비에 몇 초 걸린다)."""
        return self.worker.submit(self.flow_app)

    def check(self, body) -> tuple[str, str | None, bool]:
        if not isinstance(body, dict):
            raise ValueError("요청 형식을 확인해 주세요.")
        question, company = body.get("question"), body.get("company") or None
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError("질문을 1~2,000자로 적어 주세요.")
        if company is not None and company not in self.allowed:
            raise ValueError("알 수 없는 회사입니다.")
        return question.strip(), company, bool(body.get("all_companies"))

    def check_history(self, body) -> list[dict]:
        """화면이 보낸 직전 대화 (멀티턴). 서버는 저장하지 않는다. 형식이 틀린 턴은 버리고 최근 것만, 길이도 자른다."""
        turns = body.get("history") if isinstance(body, dict) else None
        if not isinstance(turns, list):
            return []
        out = []
        for t in turns[-flow.HISTORY_TURNS:]:
            if not isinstance(t, dict) or not isinstance(t.get("question"), str) or not t["question"].strip():
                continue
            answer, company = t.get("answer"), t.get("company")
            out.append({"question": t["question"].strip()[:2000],
                        "answer": answer[:flow.HISTORY_ANSWER_CHARS] if isinstance(answer, str) else "",
                        "company": company if company in self.allowed else None})
        return out

    def ask_stream(self, question: str, company: str | None, all_companies: bool = False, history: list | None = None):
        """이벤트를 차례로 내놓는다: status … step(running/done) … result 또는 error. 대기열까지 차면 BlockingIOError."""
        if not self.slots.acquire(blocking=False):
            raise BlockingIOError("지금 질문이 많아 받을 수 없습니다. 잠시 후 다시 보내 주세요.")
        events: queue.Queue = queue.Queue()
        with self.count_lock:
            ahead = self.inflight - self.workers + 1       # 이 질문 앞에서 기다리는 질문 수 (0 이하면 바로 시작)
            self.inflight += 1
        if ahead > 0:
            events.put({"type": "status", "message": f"다른 질문을 처리하고 있어 잠시 기다립니다… (앞에 {ahead}개)"})
        if self.app is None:                  # 준비(warm)가 아직 안 끝났으면 기다리는 이유를 먼저 알린다
            events.put({"type": "status", "message": "검색기를 준비하고 있습니다… (서버를 켠 뒤 처음 한 번)"})

        def run():
            try:
                final, live_due = None, None
                for mode, ev in self.flow_app().stream(flow.start_state(question, company, all_companies, history),
                                                       stream_mode=["tasks", "values"]):
                    if mode == "values":
                        final = ev
                        if live_due:                 # 근거가 바뀌는 단계가 끝난 직후의 상태로 근거 목록을 보낸다
                            events.put({"type": "live", "node": live_due, "items": live_evidence(ev)})
                            live_due = None
                    elif "input" in ev:
                        events.put({"type": "step", "node": ev["name"], "label": STEP_NAMES.get(ev["name"], ev["name"]),
                                    "status": "running"})
                    else:
                        events.put({"type": "step", "node": ev["name"], "label": STEP_NAMES.get(ev["name"], ev["name"]),
                                    "status": "error" if ev.get("error") else "done",
                                    "info": step_info(ev.get("result") or {})})
                        if ev["name"] in LIVE_STEPS and not ev.get("error"):
                            live_due = ev["name"]
                events.put({"type": "result", "answer": final.get("answer", ""), "citations": final.get("citations", []),
                            "evidence": evidence(final, self.sources, self.links), "route": final.get("route"), "choices": final.get("choices", []),
                            "suggestions": final.get("suggestions", []),
                            # 추천 질문마다 가리키는 회사 (하나일 때만). 누르면 화면이 그 회사로 바꿔 묻는다
                            "suggestion_companies": [S.single_company(q) for q in final.get("suggestions", [])],
                            "question": final.get("question", question), "rewritten": final.get("rewritten", False),
                            "route_prob": final.get("route_prob"), "company_prob": final.get("company_prob"), "sufficient_prob": final.get("sufficient_prob"),
                            "sufficiency_status": final.get("sufficiency_status"), "abstain_reason": final.get("abstain_reason"),
                            "expansions": final.get("expansions", 0), "insufficient": final.get("insufficient", False),
                            "trace": final.get("trace", [])})
            except Exception as e:           # 화면에는 짧은 설명만
                logging.exception("질문 처리 실패")
                events.put({"type": "error", "message": f"처리 중 오류가 났습니다: {type(e).__name__}: {e}"})
            finally:
                events.put(None)

        def done(_):
            with self.count_lock:
                self.inflight -= 1
            self.slots.release()

        self.worker.submit(run).add_done_callback(done)   # 브라우저가 끊겨도 흐름이 끝나야 자리를 돌려준다
        while (ev := events.get()) is not None:
            yield ev


def make_handler(web: WebApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status: int, body: bytes, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status: int, obj):
            self.send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/companies":
                return self.json(200, web.companies)
            name = PAGES.get(path, path.lstrip("/"))
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self.send(404, b"not found", "text/plain; charset=utf-8")
            self.send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            if urlsplit(self.path).path != "/api/ask/stream":
                return self.send(404, b"not found", "text/plain; charset=utf-8")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 60_000:                          # 질문 + 직전 대화 3턴 (답변은 앞 600자)
                    raise ValueError("요청이 너무 깁니다.")
                body = json.loads(self.rfile.read(n) or b"null")
                question, company, all_companies = web.check(body)
                stream = web.ask_stream(question, company, all_companies, web.check_history(body))
                first = next(stream)                    # 대기열까지 차면 여기서 BlockingIOError
            except (ValueError, json.JSONDecodeError) as e:
                return self.json(400, {"error": str(e)})
            except BlockingIOError as e:
                return self.json(503, {"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for ev in _chain(first, stream):
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):   # 브라우저가 닫힘: 흐름은 끝까지 돌고 결과는 버린다
                for _ in stream:
                    pass

    return Handler


def _chain(first, rest):
    yield first
    yield from rest


def serve(web: WebApp, port: int = 8767, host: str = "127.0.0.1"):
    server = ThreadingHTTPServer((host, port), make_handler(web))
    print(f"약관 질의응답 화면: http://{host}:{port}  (Ctrl+C 로 종료)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        web.worker.shutdown(wait=False)
