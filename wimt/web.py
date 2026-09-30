"""질문 처리 흐름(flow)의 웹 화면: 회사 고르기 → 질문 → 단계별 진행(스트리밍) → 답변·인용·근거.

- 서버는 표준 라이브러리(ThreadingHTTPServer). 화면은 web_static/ 의 HTML·CSS·JS (빌드 없음).
- POST /api/ask/stream: LangGraph 를 stream_mode=["tasks", "values"] 로 돌려, 노드가 시작·끝날 때마다
  SSE 이벤트를 보낸다 (Jev 판정처럼 몇 초 걸리는 단계도 "진행 중"으로 보인다). 마지막에 답변·인용·근거.
- 흐름은 작업 스레드 하나에서만 돌린다 (SQLite 연결·로컬 임베딩을 한 스레드에서 쓰고, 질문은 한 번에 하나).
"""
import json
import logging
import queue
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
STEP_NAMES = {"classify": "분기 판정", "direct_answer": "즉시 답변", "clarify": "회사 되묻기", "retrieve": "검색", "grade": "근거 판정", "laws": "법령 조회",
              "check_context": "문맥 충분성 판정", "expand": "추가 검색", "answer": "답변", "abstain": "답변 보류"}


def companies(clauses: list[dict], sources: dict | None = None) -> list[dict]:
    """회사별 문서 목록 (최신 조항에서). sources(경로 -> 이력 DB 문서)가 있으면 원문 주소·최근 개정일도 붙인다."""
    sources = sources or {}
    docs: dict[str, dict[str, str]] = {}
    for c in clauses:
        docs.setdefault(c["path"].split("/")[0], {})[c["path"]] = c.get("doc_title") or Path(c["path"]).stem
    out = []
    for k, v in sorted(docs.items()):
        items = [{"path": p, "title": t, "source_url": (sources.get(p) or {}).get("source_url") or "",
                  "latest_version": (sources.get(p) or {}).get("latest_version") or ""} for p, t in sorted(v.items())]
        out.append({"id": k, "name": S.company_name(k), "documents": items,
                    "latest_version": max((d["latest_version"] for d in items), default="")})
    return out


def evidence(state: dict, sources: dict | None = None) -> list[dict]:
    """답변에 쓴 근거 (답변 LLM 에 넘긴 것과 같은 번호 [C1] …) 를 화면에 보여 줄 수 있게.
    정본은 각 기업의 공식 페이지이므로 근거마다 원문 주소(source_url)를 붙인다."""
    if not state.get("res"):
        return []
    sources = sources or {}
    ctx, cites = rag.context_blocks(flow.keep_only(state["res"], set(state.get("relevant", []))))
    blocks = ctx.split("\n\n---\n\n") if ctx else []
    return [{**c, "text": b.split("\n", 1)[-1] if "\n" in b else "", "head": b.split("\n", 1)[0],
             "source_url": c.get("source_url") or (sources.get(c["path"]) or {}).get("source_url") or ""}
            for c, b in zip(cites, blocks)]


def step_info(result: dict) -> dict:
    """노드가 끝났을 때 화면에 보여 줄 요약 (그 노드가 trace 에 남긴 마지막 항목)."""
    info = dict((result.get("trace") or [{}])[-1])
    info.pop("grades", None)                 # 근거별 확률은 길어서 최종 결과의 trace 로만
    return info


class WebApp:
    def __init__(self, clauses: list[dict], make_app, documents: list[dict] | None = None):
        """make_app() -> 컴파일된 흐름. 작업 스레드 안에서 처음 한 번 부른다 (임베딩 모델 등을 그 스레드에서 연다).
        documents: 이력 DB 의 문서 목록 (원문 주소 source_url, 최근 개정일 latest_version)."""
        self.sources = {d["path"]: d for d in documents or []}
        self.companies = companies(clauses, self.sources)
        self.allowed = {c["id"] for c in self.companies}
        self.make_app, self.app = make_app, None
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.busy = threading.Lock()

    def warm(self):
        """서버를 띄우자마자 작업 스레드에서 흐름을 만든다 (검색기·임베딩 준비에 몇 초 걸린다)."""
        def build():
            if self.app is None:
                self.app = self.make_app()
        return self.worker.submit(build)

    def check(self, body) -> tuple[str, str | None, bool]:
        if not isinstance(body, dict):
            raise ValueError("요청 형식을 확인해 주세요.")
        question, company = body.get("question"), body.get("company") or None
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError("질문을 1~2,000자로 적어 주세요.")
        if company is not None and company not in self.allowed:
            raise ValueError("알 수 없는 회사입니다.")
        return question.strip(), company, bool(body.get("all_companies"))

    def ask_stream(self, question: str, company: str | None, all_companies: bool = False):
        """이벤트를 차례로 내놓는다: step(running/done) … result 또는 error. busy 면 BlockingIOError."""
        if not self.busy.acquire(blocking=False):
            raise BlockingIOError("다른 질문을 처리하고 있습니다. 잠시 후 다시 보내 주세요.")
        events: queue.Queue = queue.Queue()
        if self.app is None:                  # 준비(warm)가 아직 안 끝났으면 기다리는 이유를 먼저 알린다
            events.put({"type": "status", "message": "검색기를 준비하고 있습니다… (서버를 켠 뒤 처음 한 번)"})

        def run():
            try:
                if self.app is None:
                    self.app = self.make_app()
                final = None
                for mode, ev in self.app.stream(flow.start_state(question, company, all_companies),
                                                stream_mode=["tasks", "values"]):
                    if mode == "values":
                        final = ev
                    elif "input" in ev:
                        events.put({"type": "step", "node": ev["name"], "label": STEP_NAMES.get(ev["name"], ev["name"]),
                                    "status": "running"})
                    else:
                        events.put({"type": "step", "node": ev["name"], "label": STEP_NAMES.get(ev["name"], ev["name"]),
                                    "status": "error" if ev.get("error") else "done",
                                    "info": step_info(ev.get("result") or {})})
                events.put({"type": "result", "answer": final.get("answer", ""), "citations": final.get("citations", []),
                            "evidence": evidence(final, self.sources), "route": final.get("route"), "choices": final.get("choices", []),
                            "route_prob": final.get("route_prob"), "company_prob": final.get("company_prob"), "sufficient_prob": final.get("sufficient_prob"),
                            "sufficiency_status": final.get("sufficiency_status"),
                            "expansions": final.get("expansions", 0), "insufficient": final.get("insufficient", False),
                            "trace": final.get("trace", [])})
            except Exception as e:           # 화면에는 짧은 설명만
                logging.exception("질문 처리 실패")
                events.put({"type": "error", "message": f"처리 중 오류가 났습니다: {type(e).__name__}: {e}"})
            finally:
                events.put(None)

        try:
            self.worker.submit(run)
            while (ev := events.get()) is not None:
                yield ev
        finally:
            self.busy.release()


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
                if n > 20_000:
                    raise ValueError("요청이 너무 깁니다.")
                question, company, all_companies = web.check(json.loads(self.rfile.read(n) or b"null"))
                stream = web.ask_stream(question, company, all_companies)
                first = next(stream)                    # busy 면 여기서 BlockingIOError
            except (ValueError, json.JSONDecodeError) as e:
                return self.json(400, {"error": str(e)})
            except BlockingIOError as e:
                return self.json(409, {"error": str(e)})
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
