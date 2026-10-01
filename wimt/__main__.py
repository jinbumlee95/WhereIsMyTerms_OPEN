"""명령줄: python -m wimt <명령> [옵션]

  registry        소스 레지스트리·데이터 카드 생성 (data/sources.yaml)
  diagnose        약관 페이지 HTTP 수집 가능성 진단 (reports/diagnose.csv)
  stats           청킹 전략 비교 조항 통계표 (reports/clause_stats.csv)
  scan            변경 판정 파이프라인 실행 (reports/scan/*.jsonl, reports/alerts.md)
  search          '지금 불리한 조항' / '불리해진 조항' 검색 (scan 결과 사용)
  review          변경 수동 검사표 (reports/change_review.csv)
  label-sheet     유불리 라벨링 시트 (reports/labeling_sheet.csv)
  label-compare   라벨링 결과와 지수 비교, 기준값 제안 (reports/labeling_compare.csv)
  ui              검사표·라벨링·평가 질문을 브라우저에서 버튼으로 채우는 피드백 UI (http://127.0.0.1:8765)
  qa-draft        RAG 평가 질문 초안 생성 (reports/qa_sheet.csv, LLM)
  qa-vague        채택된 평가 질문마다 약관을 모르는 사용자 말투의 짝을 추가 (정답은 같음)
  qa-history      이력 평가 질문 초안을 유형별로 추가 (날짜 지정·날짜 없음·삭제·번호 변경·문서 개정)
  rag-eval        채택한 평가 질문으로 검색 방식별 recall@k·MRR (reports/rag_eval.csv)
  answer          질문에 약관 조항·변경 이력을 근거로 답변 (RAG, 질문 분기 + 변경 이력 DB)
  flow            질문 처리 흐름 (LangGraph): Jev 분기 → 검색 → Jev 근거 판정 → (인용 법령 조회) → 부족하면 추가 검색 → 답변
  laws            최신 조항이 인용한 법령 통계, --fetch [--history] 로 인용된 법령 파일을 legalize-kr 에서 미리 받음 (.cache/laws/)
  flow-diagram    질문 처리 흐름 도식도 (docs/rag-pipeline.png)
  web             질문 처리 흐름의 웹 화면 (http://127.0.0.1:8767, 단계별 진행을 스트리밍)
  db              변경 이력 DB 생성 (.cache/history.sqlite3, index 가 함께 만든다)
  timeline        조항 하나의 변경 이력 (DB 조회)
  eval            쿠팡 제38조 면책 조항 재현 시나리오
  index           최신 조항과 변경 이력을 임베딩해 Chroma 벡터 DB 에 색인 (.chroma/, scan 결과 사용)
  ftc             공정위 시정 사례를 색인하고 조항마다 문구가 비슷한 사례를 연결 (index 끝에도 돈다)
  ask             벡터 DB 의미 검색 (서비스·문서 종류·불리한 조항 필터)

키는 환경 변수나 프로젝트 폴더의 .env 에서 읽는다 (OPENAI_API_KEY, TYPESAFE_API_KEY, 법령 조회용 GITHUB_TOKEN).
기본 임베딩 모델은 WIMT_EMBED_MODEL 로 바꾼다 (GPU 없는 PC: text-embedding-3-small 등). --model 이 우선한다.
OpenAI 색인은 예상 토큰이 WIMT_EMBED_MAX_TOKENS (기본 200000) 를 넘으면 --yes 없이는 멈춘다.
"""
import argparse
import json
import sys
from pathlib import Path

from . import pipeline, repo as R, reports
from .env import load_env
from .index import ModelError, default_model
from .score import make_scorer

ROOT = Path(__file__).resolve().parents[1]
REPORTS, DATA, CACHE, CHROMA = ROOT / "reports", ROOT / "data", ROOT / ".cache", ROOT / ".chroma"
HISTORY_DB = CACHE / "history.sqlite3"


def _slug(path: str) -> str:
    return path.removesuffix(".md").replace("/", "__")


def _select(args) -> list[str]:
    docs = R.documents(args.repo)
    if args.docs:
        docs = [d for d in docs if any(d.startswith(p) or d == p for p in args.docs)]
    return docs


def cmd_registry(args):
    import yaml
    cards = [R.data_card(p, args.repo) for p in _select(args)]
    DATA.mkdir(exist_ok=True)
    (DATA / "sources.yaml").write_text(yaml.safe_dump(cards, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"데이터 카드 {len(cards)}개 -> {DATA / 'sources.yaml'}")


def cmd_diagnose(args):
    from .diagnose import diagnose
    sources = []
    for p in _select(args):
        v = R.history(p, args.repo)[-1]
        sources.append({"service": R.service_of(p), "path": p, "url": v.meta.get("출처"), "body": v.body})
    rows = diagnose(sources, delay=args.delay)
    out = reports.write_csv(REPORTS / "diagnose.csv", rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["method"]] = counts.get(r["method"], 0) + 1
    print(f"URL {len(rows)}개 -> {out}")
    for k, n in sorted(counts.items()):
        print(f"  {k}: {n}")


def cmd_stats(args):
    docs = [(p, R.history(p, args.repo)[-1].body) for p in _select(args)]
    rows = reports.clause_stats(docs)
    out = reports.write_csv(REPORTS / "clause_stats.csv", rows)
    print(reports.summarize_stats(rows))
    print(f"-> {out}")


def cmd_scan(args):
    scorer = make_scorer(args.scorer, CACHE)
    out_dir = REPORTS / "scan" / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)
    alerts = []
    for p in _select(args):
        res = pipeline.scan(p, scorer, args.strategy, args.repo)
        with (out_dir / f"{_slug(p)}.jsonl").open("w", encoding="utf-8") as f:
            for rec in res.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with (out_dir / f"{_slug(p)}.current.jsonl").open("w", encoding="utf-8") as f:
            for c in res.current:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        alerts += [r for r in res.records if r["became_unfavorable"]]
        print(f"{p}: 버전 {res.card['change_count'] + 1}, 기록 {len(res.records)}, "
              f"불리해진 조항 {sum(r['became_unfavorable'] for r in res.records)}")
    _write_alerts(alerts, REPORTS / "alerts.md")
    print(f"채점: 캐시 {scorer.hits}건, 새로 {scorer.misses}건, 긴 목록 제외 {scorer.skipped}건, "
          f"창으로 나눈 항 {scorer.windowed}건 ({scorer.name}) · 알림 {len(alerts)}건 -> {REPORTS / 'alerts.md'}")


def _write_alerts(alerts: list[dict], path: Path):
    from .score import DISCLAIMER
    lines = ["# 불리해진 조항", "", f"> {DISCLAIMER}", "",
             f"총 {len(alerts)}건. 채점기: {alerts[0]['scorer'] if alerts else '-'}", ""]
    for r in sorted(alerts, key=lambda r: r["version_date"], reverse=True):
        kind = "불리한 조항 추가" if r["change_type"] == "added" else "불리하게 수정"
        triggers = [u for u in r["unit_changes"] if u["flag"] == "became"]
        lines += [f"## {r['doc_title']} {r['clause_id']} — {kind}",
                  f"- 서비스: {r['service']} · 문서: `{r['path']}` · 버전: {r['version_date']} (`{r['commit'][:10]}`)",
                  f"- 지수: {r['favor_score']:+.2f} (신뢰도 {r['favor_confidence']:.2f})"
                  + (f" · 시행일 {r['effective_date']}" if r['effective_date'] else "")
                  + (f" · 행동 기한 {r['action_deadline']}" if r['action_deadline'] else ""),
                  *[f"- 원인: {u['clause_id']} "
                    + ("항 추가, 지수 " if u["change_type"] == "added" else f"항 지수 {u['old_score']:+.2f} → ")
                    + f"{u['favor_score']:+.2f}" for u in triggers],
                  "", "> " + "\n\n".join(u["text"] for u in triggers).replace("\n", "\n> ")[:800], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _load(kind: str, strategy: str) -> list[dict]:
    rows = []
    for f in sorted((REPORTS / "scan" / strategy).glob(f"*{'.current' if kind == 'current' else ''}.jsonl")):
        if kind == "records" and f.name.endswith(".current.jsonl"):
            continue
        # splitlines() 는 본문 속 U+2028 등에서도 끊으므로 \n 으로만 나눈다
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").split("\n") if line]
    if not rows:
        sys.exit("scan 결과가 없습니다. 먼저 python -m wimt scan 을 실행하세요.")
    return rows


def cmd_search(args):
    if args.became:
        rows = [r for r in _load("records", args.strategy) if r["became_unfavorable"]]
    else:
        rows = [r for r in _load("current", args.strategy) if r["favor_score"] <= args.threshold]
    if args.service:
        rows = [r for r in rows if r["service"].startswith(args.service)]
    if args.q:
        rows = [r for r in rows if args.q in r["text"] or args.q in (r.get("title") or "")]
    rows.sort(key=lambda r: (r["favor_score"], r.get("version_date", "")))
    for r in rows[:args.limit]:
        print(f"{r['favor_score']:+.2f}  {r['path']}  {r['clause_id']}  ({r.get('version_date', '')})")
        print("       " + r["text"].replace("\n", " ")[:140])
    print(f"총 {len(rows)}건" + (f" (상위 {args.limit}건 표시)" if len(rows) > args.limit else ""))
    from .score import DISCLAIMER
    print(f"※ {DISCLAIMER}")


def _guard(path: Path, col: str, force: bool):
    """이미 답을 채운 시트는 --force 없이 덮어쓰지 않는다."""
    if path.exists() and not force:
        done = sum(1 for r in reports.read_csv(path) if (r.get(col) or "").strip())
        if done:
            sys.exit(f"{path.name} 에 이미 {done}건 입력됨. 덮어쓰려면 --force")


def cmd_review(args):
    from .ui import SHEETS
    for name in ("change", "drop"):
        _guard(REPORTS / SHEETS[name][0], SHEETS[name][1], args.force)
    records = _load("records", args.strategy)
    rows = reports.change_review(records, n=args.n)
    print(f"변경 무작위 {len(rows)}건 (가짜 변경 비율 확인) -> {reports.write_csv(REPORTS / 'change_review.csv', rows)}")
    drops = reports.drop_review(records)
    print(f"지수 하락 {len(drops)}건 (채점 흔들림 확인) -> {reports.write_csv(REPORTS / 'drop_review.csv', drops)}")
    print("python -m wimt ui 로 브라우저에서 채울 수 있습니다.")


def cmd_label_sheet(args):
    from .ui import SHEETS
    _guard(REPORTS / SHEETS["label"][0], SHEETS["label"][1], args.force)
    rows = reports.labeling_sheet(_load("current", args.strategy), n=args.n)
    print(f"항 {len(rows)}개 -> {reports.write_csv(REPORTS / 'labeling_sheet.csv', rows)}")
    print("python -m wimt ui 로 라벨을 채운 뒤 비교하거나, '사람라벨(-2~2)' 열을 직접 채우고 label-compare 를 실행하세요.")


def _label_compare(sheet: Path, scorer: str):
    rows, summary = reports.labeling_compare(reports.read_csv(sheet), make_scorer(scorer, CACHE))
    if rows:
        reports.write_csv(REPORTS / "labeling_compare.csv", rows)
    return rows, summary


def cmd_label_compare(args):
    _, summary = _label_compare(args.sheet, args.scorer)
    print(json.dumps(summary, ensure_ascii=False, indent=2))




def cmd_ui(args):
    from .ui import App, serve
    builders = {
        "change": lambda: reports.change_review(_load("records", args.strategy)),
        "drop": lambda: reports.drop_review(_load("records", args.strategy)),
        "label": lambda: reports.labeling_sheet(_load("current", args.strategy)),
    }
    serve(App(REPORTS, builders, lambda scorer: _label_compare(REPORTS / "labeling_sheet.csv", scorer)),
          port=args.port)


def cmd_eval(args):
    """쿠팡 이용약관 제38조: 2024-11-05 면책 항 추가가 '불리해진 조항'으로, 2025-12-26 삭제가 '불리한 조항 삭제'로 잡히는가."""
    res = pipeline.scan("coupang/쿠팡이용약관.md", make_scorer(args.scorer, CACHE), args.strategy, args.repo)
    art = [r for r in res.records if r["article"] == "제38조"]

    def hit(date, flag):
        return [(r, u) for r in art if r["version_date"] == date for u in r["unit_changes"]
                if u["flag"] == flag and "불법적인 접속" in u["text"]]
    added, removed = hit("2024-11-05", "became"), hit("2025-12-26", "removed")
    for name, h in (("2024-11-05 면책 항 추가 → 불리해진 조항", added), ("2025-12-26 면책 항 삭제 → 불리한 조항 삭제", removed)):
        detail = f"  ({h[0][1]['clause_id']}, 항 지수 {h[0][1]['favor_score'] if h[0][1]['favor_score'] is not None else h[0][1]['old_score']})" if h else ""
        print(("PASS " if h else "FAIL ") + name + detail)
    sys.exit(0 if added and removed else 1)


def cmd_index(args):
    from . import index as I
    clauses, records = _load("current", args.strategy), _load("records", args.strategy)
    I.check_model(args.model)
    # 비용 확인: 임베딩하기 전에 새로 임베딩할 조각의 예상 토큰을 보고, 한도를 넘으면 멈춘다
    todo = (I.pending(I.entries(clauses), CHROMA, I.collection_name("clauses", args.strategy, args.model), args.model)
            + I.pending(I.change_entries(records), CHROMA, I.collection_name("changes", args.strategy, args.model),
                        args.model))
    print(f"임베딩 모델: {args.model} ({I.model_source(args.model)}) · 새로 임베딩할 조각 {len(todo)}개 · "
          + I.check_budget(args.model, I.estimate_tokens(todo), args.yes))
    emb = I.make_embedder(args.model)
    if getattr(emb, "device", None):
        print(f"임베딩: {emb.model} ({emb.device})")
    stats = I.build(clauses, CHROMA, args.strategy, emb)
    print(f"{stats['collection']}: 조항 {stats['clauses']}개 -> 조각 {stats['total']}개 (여러 조각으로 나눈 조항 {stats['split']}개) · "
          f"새로 임베딩 {stats['embedded']}, 메타데이터만 갱신 {stats['metadata_only']}, 삭제 {stats['deleted']} · "
          f"임베딩 토큰 {stats['tokens']:,} -> {CHROMA}")
    ch = I.build_changes(records, CHROMA, args.strategy, emb)
    print(f"{ch['collection']}: 변경 레코드 {ch['total']}개 · 새로 임베딩 {ch['embedded']}, "
          f"메타데이터만 갱신 {ch['metadata_only']}, 삭제 {ch['deleted']} · 임베딩 토큰 {ch['tokens']:,}")
    _ftc_link(args, emb)
    cmd_db(args)


def _ftc_link(args, emb=None):
    """공정위 시정 사례를 색인하고 조항마다 문구가 비슷한 사례를 연결한다. 사례 파일이 없으면 건너뛴다."""
    from . import ftc, index as I
    if not ftc.available():
        print(f"공정위 시정 사례: {ftc.PAIRS} 또는 {ftc.CASES_CSV} 가 없어 건너뜁니다 (tools/ftc_eval.py extract)")
        return
    todo = I.pending(ftc.entries(ftc.load_cases()), CHROMA, ftc.collection(args.model), args.model)
    if todo:
        print(f"공정위 시정 사례: 새로 임베딩 {len(todo)}개 · "
              + I.check_budget(args.model, I.estimate_tokens(todo), getattr(args, "yes", False)))
    emb = emb or I.make_embedder(args.model)
    st = ftc.build(CHROMA, emb)
    ln = ftc.link(CHROMA, CACHE, args.strategy, args.model)
    print(f"{st['collection']}: 사례 {st['total']}개 (새로 임베딩 {st['embedded']}) · 조항 {ln['clauses']}개 중 "
          f"{ln['linked']}개에 유사 사례 연결 (유사도 {ftc.LINK_MIN} 이상, 사례 {ln['cases']}개 사용) -> {ln['path']}")


def cmd_ftc(args):
    from . import index as I
    I.check_model(args.model)
    _check_embeddings(args, kinds=("clauses",))
    _ftc_link(args)


def cmd_db(args):
    from . import db
    stats = db.build(HISTORY_DB, _load("records", args.strategy), _load("current", args.strategy))
    print(f"변경 이력 DB: 문서 {stats['documents']}, 버전 {stats['versions']}, 조항 변경 {stats['changes']}, "
          f"항 변경 {stats['unit_changes']}, 최신 조항 {stats['clauses']} -> {HISTORY_DB}")


def cmd_timeline(args):
    from .db import DB
    rows = DB(HISTORY_DB).timeline(args.path, args.clause)
    for c in rows:
        print(c["change_text"][:args.chars])
        print()
    print(f"{args.path} {args.clause}: 변경 {len(rows)}건")


def _check_embeddings(args, kinds=("clauses", "changes")):
    """검색 전 확인: 모델을 쓸 수 있고, 그 모델로 만든 색인이 지금 scan 결과와 맞는가. 아니면 ModelError."""
    from . import index as I
    I.check_model(args.model)
    ids = {"clauses": lambda: I.vector_ids(I.entries(_load("current", args.strategy))),
           "changes": lambda: I.vector_ids(I.change_entries(_load("records", args.strategy)))}
    for kind in kinds:
        I.check_index(CHROMA, kind, args.strategy, args.model, ids[kind]())
    print(f"(임베딩 모델: {args.model}, {I.model_source(args.model)})", file=sys.stderr)


def _retriever(args):
    from .index import make_embedder
    from .rag import Retriever
    from .db import DB
    _check_embeddings(args)
    if not HISTORY_DB.exists():
        cmd_db(args)
    return Retriever(_load("current", args.strategy), _load("records", args.strategy), CHROMA, args.strategy,
                     make_embedder(args.model), DB(HISTORY_DB))


def cmd_qa_draft(args):
    from .rag import LLM, qa_candidates, qa_draft
    from .ui import SHEETS
    _guard(REPORTS / SHEETS["qa"][0], SHEETS["qa"][1], args.force)
    cands = qa_candidates(_load("current", args.strategy), _load("records", args.strategy), args.current, args.history)
    llm = LLM(args.llm)
    rows = qa_draft(cands, llm)
    print(f"질문 초안 {len(rows)}개 (대상 {len(cands)}개, 토큰 {llm.tokens:,}) -> "
          f"{reports.write_csv(REPORTS / SHEETS['qa'][0], rows)}")
    print("python -m wimt ui 의 '평가 질문' 탭에서 채택·수정·버림을 고른 뒤 python -m wimt rag-eval 을 실행하세요.")


def cmd_qa_history(args):
    """이력 평가 질문 초안을 유형별로 만들어 평가 시트 뒤에 붙인다 (기존 판정은 그대로)."""
    from .db import DB
    from .rag import LLM, qa_history_candidates, qa_history_draft
    from .ui import SHEETS
    path = REPORTS / SHEETS["qa"][0]
    rows = reports.read_csv(path) if path.exists() else []
    counts = {"dated": args.dated, "undated": args.undated, "deleted": args.deleted, "renumbered": args.renumbered,
              "doc": args.doc}
    if not HISTORY_DB.exists():
        cmd_db(args)
    cands = qa_history_candidates(_load("records", args.strategy), _load("current", args.strategy), DB(HISTORY_DB), counts)
    llm = LLM(args.llm)
    start = max((int(r["id"]) for r in rows), default=0) + 1
    new = qa_history_draft(cands, llm, start)
    reports.write_csv(path, rows + new)
    by = {}
    for r in new:
        by[r["htype"]] = by.get(r["htype"], 0) + 1
    print(f"이력 질문 초안 {len(new)}개 {by} (토큰 {llm.tokens:,}) -> {path} (id {start}~)")
    print("python -m wimt ui 의 '평가 질문' 탭에서 검수하세요.")


def cmd_qa_vague(args):
    """채택된 질문마다 약관을 모르는 사용자 말투의 짝을 만들어 평가 시트 뒤에 붙인다."""
    from .rag import LLM, overlap, qa_vague
    from .ui import SHEETS
    path = REPORTS / SHEETS["qa"][0]
    rows = reports.read_csv(path)
    llm = LLM(args.llm)
    new = qa_vague(rows, llm)
    reports.write_csv(path, rows + new)
    src = {r["id"]: r for r in rows}
    before = [overlap(src[r["src_id"]]["question"], r["text"]) for r in new]
    after = [overlap(r["question"], r["text"]) for r in new]
    if new:
        print(f"모호한 질문 {len(new)}개 (토큰 {llm.tokens:,}) -> {path}")
        print(f"질문 글자 2-gram 중 정답 텍스트에 있는 비율: 원래 {sum(before) / len(new):.2f} -> 모호 {sum(after) / len(new):.2f}")
    print("ui '평가 질문' 탭에서 검수 후 python -m wimt rag-eval --route --variant vague")


def cmd_rag_eval(args):
    from .rag import evaluate
    from .ui import SHEETS
    from .rag import LLM
    llm = LLM(args.llm) if (args.rewrite or args.route) else None
    rows = [r for r in reports.read_csv(REPORTS / SHEETS["qa"][0])
            if args.variant == "all" or (r.get("variant") or "orig") == args.variant]
    summary, detail = evaluate(rows, _retriever(args), args.modes,
                               auto_company=not args.no_service_filter, rewrite=llm if args.rewrite else None,
                               route=llm if args.route else None,
                               route_variants=None)
    if detail:
        reports.write_csv(REPORTS / "rag_eval.csv", detail)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _laws(args):
    """법령 조회기 (legalize-kr). --no-laws 면 None (흐름은 법령 조회를 건너뛴다)."""
    import os
    from . import laws as L
    if getattr(args, "no_laws", False):
        return None
    if not os.environ.get("GITHUB_TOKEN"):
        print("알림: GITHUB_TOKEN 이 없어 법령 개정 이력 조회가 GitHub API 시간당 60회로 제한됩니다 (법령 하나에 1회).",
              file=sys.stderr)
    return L.Client()


def cmd_laws(args):
    """최신 조항이 인용한 법령 통계. --fetch 면 인용된 법령 파일(그 날짜에 시행 중인 판)을 미리 받아 캐시한다.
    --history 면 변경 기록의 버전 날짜 기준 판도 받는다 (날짜 지정 이력 질문이 빨라진다)."""
    import time
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from . import laws as L
    counts, named = Counter(), Counter()
    wanted: set[tuple[str, str, str]] = set()          # (법령, 구분, 기준일)
    for c in _load("current", args.strategy):
        for r in L.extract(c["text"]):
            counts[f"{L.display(r.law)} ({r.category})"] += 1
            wanted.add((r.law, r.category, L.CURRENT))
        for r, _ in L.extract_names(c["text"]):
            named[f"{L.display(r.law)} ({r.category})"] += 1
            wanted.add((r.law, r.category, L.CURRENT))
    print(f"조 번호 인용 {sum(counts.values())}건 (법령 {len(counts)}개), 이름만 인용 {sum(named.values())}건 (법령 {len(named)}개)")
    for name, n in (counts + named).most_common(args.top):
        print(f"  {n:4d}  {name}")
    if not args.fetch:
        return
    if args.history:
        for r in _load("records", args.strategy):
            text = "\n".join(u.get("text") or "" for u in r.get("unit_changes") or []) or r.get("text") or ""
            for ref in L.extract(text, r["version_date"]) + [x for x, _ in L.extract_names(text, r["version_date"])]:
                wanted.add((ref.law, ref.category, ref.as_of))
    client = _laws(args)
    if client is None:
        sys.exit(2)
    todo = sorted(wanted)
    print(f"법령 판 {len(todo)}개 확인 (법령 파일 {len({(l, c) for l, c, _ in todo})}개)")
    t0, found, missing, failed = time.perf_counter(), 0, 0, 0

    def one(item):
        return client.headings(L.Ref(item[0], item[1], "", item[2]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        for item, fut in zip(todo, [pool.submit(one, t) for t in todo]):
            try:
                ok = bool(fut.result())
            except L.RateLimited as e:
                print(f"한도 초과로 멈춤: {e}", file=sys.stderr)
                break
            except L.LawError as e:
                failed += 1
                print(f"  실패 {item}: {e}", file=sys.stderr)
                continue
            found, missing = found + ok, missing + (not ok)
            if not ok:
                print(f"  없음 {L.display(item[0])} ({item[1]}) @ {item[2]}")
    print(f"찾음 {found}, 없음 {missing}, 실패 {failed} ({time.perf_counter() - t0:.1f}초) -> {client.cache_dir}")


def cmd_flow(args):
    from . import flow
    from .rag import LLM
    app = flow.build(_retriever(args), LLM(args.llm), flow.Judge(), mode=args.mode, laws=_laws(args))
    out = flow.run(app, args.question, args.service)
    print(out["answer"])
    print()
    if args.trace:
        for t in out["trace"]:
            print("·", json.dumps(t, ensure_ascii=False)[:600])
    else:
        print(" → ".join(t["step"] for t in out["trace"])
              + (f" (분기 확률 {out['route_prob']:.2f}" + (", 충분성 판정 실패" if out.get("sufficiency_status") == "unknown"
                   else f", 충분 {out['sufficient_prob']:.2f}" if "sufficient_prob" in out else "") + ")"))
    if out.get("insufficient"):
        print({"not_found": "(약관에서 찾지 못함: 답변 보류)", "unknown": "(판정 오류: 답변 보류)"}
              .get(out.get("abstain_reason"), "(근거 부족: 답변 보류)"))
    for c in out["citations"]:
        kind = f" {c['change_type']}" if "change_type" in c else ""
        print(f"[{c['tag']}] {c['path']} {c['clause_id']} ({c['version_date']}{kind})")
    if out.get("suggestions"):
        print("\n이어서 물어보기: " + " / ".join(out["suggestions"]))


def cmd_web(args):
    from . import flow, web
    from .rag import LLM
    _check_embeddings(args)                       # 모델·색인 문제는 서버를 띄우기 전에 알린다
    judge = flow.Judge()                          # TYPESAFE_API_KEY 가 없으면 여기서 멈춘다
    laws = _laws(args)

    def make_app():                               # 작업 스레드 안에서 한 번 (임베딩 모델을 그 스레드에서 연다)
        return flow.build(_retriever(args), LLM(args.llm), judge, mode=args.mode, laws=laws)

    from .db import DB
    if not HISTORY_DB.exists():
        cmd_db(args)
    from . import ftc
    links = ftc.Links(ftc.links_path(CACHE, args.strategy, args.model))
    print(f"공정위 유사 시정 사례: 조항 {len(links.links)}개에 연결" if links else
          "공정위 유사 시정 사례: 연결 파일이 없어 표시하지 않습니다 (python -m wimt ftc)")
    app = web.WebApp(_load("current", args.strategy), make_app, DB(HISTORY_DB).documents(), links=links)
    app.warm()
    web.serve(app, port=args.port)


def cmd_flow_diagram(args):
    from . import flow
    app = flow.build(None, None, None)
    out = ROOT / "docs" / "rag-pipeline.png"
    flow.draw(app, out)
    (ROOT / "docs" / "rag-pipeline.mmd").write_text(app.get_graph().draw_mermaid(), encoding="utf-8")
    print(f"도식도 -> {out} (Mermaid 원본: docs/rag-pipeline.mmd)")


def cmd_answer(args):
    from .rag import LLM, answer
    out = answer(args.question, _retriever(args), LLM(args.llm), rewrite=not args.no_rewrite, route=not args.no_route,
                 k=args.k, k_changes=args.k_changes, mode=args.mode, company=args.service,
                 auto_company=not args.no_service_filter)
    print(out["answer"])
    print()
    if out.get("plan"):
        p = out["plan"]
        print(f"(분기: {p['intent']}" + (f", 기간 {p['date_from']}~{p['date_to']}" if p["date_from"] else "") + ")")
    if len(out["queries"]) > 1:
        print(f"(검색어: {' / '.join(out['queries'][1:])})")
    if out["filter"]:
        print(f"(검색 필터: {out['filter']})")
    for c in out["citations"]:
        kind = f" {c['change_type']}" if "change_type" in c else ""
        print(f"[{c['tag']}] {c['path']} {c['clause_id']} ({c['version_date']}{kind})")


def cmd_ask(args):
    from .index import make_embedder, search
    _check_embeddings(args, kinds=("clauses",))
    rows = search(args.query, CHROMA, args.strategy, make_embedder(args.model), k=args.k, service=args.service,
                  doc_type=args.doc_type, unfavorable=args.unfavorable, with_refs=args.refs)
    for r in rows:
        where = (f"  조각 {','.join(map(str, r['hit_pieces']))}/{r['pieces']}에서 걸림"
                 + ("" if r["complete"] else f", 앞뒤 포함 {len(r['shown_pieces'])}개만") if r["pieces"] > 1 else "")
        print(f"{r['similarity']:.3f}  {r['clause_score']:+.2f}  {r['path']}  {r['clause_id']}  ({r['version_date']}){where}")
        text = r["text"].replace("\n", " ")
        print("       " + (text if args.full else text[:160]))
        for rel in r["related"]:
            t = rel["text"].replace("\n", " ")
            print(f"       ↳ 참조 {rel['clause_id']} {rel['title']}: " + (t if args.full else t[:100]))
    if not rows:
        print("결과 없음 (index 를 먼저 실행했는지, 필터가 너무 좁지 않은지 확인하세요)")
    else:
        from .score import DISCLAIMER
        print(f"※ 두 번째 열은 유불리 지수. {DISCLAIMER}")


def main(argv=None):
    load_env(ROOT / ".env")
    p = argparse.ArgumentParser(prog="wimt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=R.DEFAULT_REPO, help="TOS-Korea-Project 경로")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, docs=True, strategy=False, scorer=False):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if docs:
            sp.add_argument("docs", nargs="*", help="문서 경로 또는 접두어 (예: coupang/, krafton/pubg/)")
        if strategy:
            sp.add_argument("--strategy", choices=["article", "paragraph", "heading"], default="article")
        if scorer:
            sp.add_argument("--scorer", choices=["baseline", "jev"], default="baseline")
        return sp

    add("registry", cmd_registry)
    add("diagnose", cmd_diagnose).add_argument("--delay", type=float, default=1.0)
    add("stats", cmd_stats)
    add("scan", cmd_scan, strategy=True, scorer=True)
    s = add("search", cmd_search, docs=False, strategy=True)
    s.add_argument("--became", action="store_true", help="불리해진 조항 (기본: 지금 불리한 조항)")
    s.add_argument("--threshold", type=float, default=pipeline.UNFAVORABLE)
    s.add_argument("--service"); s.add_argument("-q", "--q"); s.add_argument("--limit", type=int, default=20)
    rv = add("review", cmd_review, docs=False, strategy=True)
    rv.add_argument("-n", type=int, default=25); rv.add_argument("--force", action="store_true")
    ls = add("label-sheet", cmd_label_sheet, docs=False, strategy=True)
    ls.add_argument("-n", type=int, default=50); ls.add_argument("--force", action="store_true")
    add("ui", cmd_ui, docs=False, strategy=True).add_argument("--port", type=int, default=8765)
    lc = add("label-compare", cmd_label_compare, docs=False, scorer=True)
    lc.add_argument("--sheet", type=Path, default=REPORTS / "labeling_sheet.csv")
    add("eval", cmd_eval, docs=False, strategy=True, scorer=True)
    ix = add("index", cmd_index, docs=False, strategy=True)
    ix.add_argument("--model", default=default_model(), help="임베딩 모델 (OpenAI: text-embedding-3-small). 기본은 WIMT_EMBED_MODEL")
    ix.add_argument("--yes", action="store_true", help="예상 토큰이 WIMT_EMBED_MAX_TOKENS 를 넘어도 OpenAI 로 색인")
    ak = add("ask", cmd_ask, docs=False, strategy=True)
    ak.add_argument("query")
    ak.add_argument("-k", type=int, default=10)
    ak.add_argument("--service", help="회사(coupang) 또는 서비스(krafton/pubg)")
    ak.add_argument("--doc-type", help="약관, 운영정책, 부가서비스, 개인정보처리방침, 대회 규정, 동의서, 기타")
    ak.add_argument("--unfavorable", action="store_true", help=f"불리한 조항(지수 ≤ {pipeline.UNFAVORABLE})만")
    ak.add_argument("--refs", action="store_true", help="조항이 가리키는 같은 문서의 다른 조도 함께")
    ak.add_argument("--full", action="store_true", help="조항 전체 텍스트 출력 (기본: 앞부분만)")
    ak.add_argument("--model", default=default_model(), help="임베딩 모델 (index 와 같아야 함)")

    def rag_opts(sp):
        sp.add_argument("--model", default=default_model(), help="임베딩 모델 (index 와 같아야 함)")
        sp.add_argument("--no-service-filter", action="store_true", help="질문 속 서비스 이름으로 거르지 않음")
        return sp

    qd = add("qa-draft", cmd_qa_draft, docs=False, strategy=True)
    qd.add_argument("--current", type=int, default=40)
    qd.add_argument("--history", type=int, default=20)
    qd.add_argument("--llm", default="gpt-5.4-mini")
    qd.add_argument("--force", action="store_true")
    add("qa-vague", cmd_qa_vague, docs=False).add_argument("--llm", default="gpt-5.4-mini")
    qh = add("qa-history", cmd_qa_history, docs=False, strategy=True)
    for name, n in (("dated", 6), ("undated", 5), ("deleted", 4), ("renumbered", 2), ("doc", 3)):
        qh.add_argument(f"--{name}", type=int, default=n)
    qh.add_argument("--llm", default="gpt-5.4-mini")
    ev = rag_opts(add("rag-eval", cmd_rag_eval, docs=False, strategy=True))
    ev.add_argument("--modes", nargs="+", default=["vector", "bm25", "hybrid"], choices=["vector", "bm25", "hybrid"])
    ev.add_argument("--rewrite", action="store_true", help="질문 재작성(LLM)을 켠 방식도 함께 잰다")
    ev.add_argument("--route", action="store_true", help="질문 분기 + 변경 이력 DB 조회 방식의 근거 포함률도 잰다")
    ev.add_argument("--variant", default="orig", choices=["orig", "vague", "all"],
                    help="원래 질문(orig) / 사용자 말투로 흐린 질문(vague, qa-vague 로 생성)")
    ev.add_argument("--llm", default="gpt-5.4-mini")
    an = rag_opts(add("answer", cmd_answer, docs=False, strategy=True))
    an.add_argument("question")
    an.add_argument("-k", type=int, default=5, help="근거로 줄 현재 조항 수")
    an.add_argument("--k-changes", type=int, default=5, help="근거로 줄 변경 이력 수 (0 이면 현재 약관만)")
    an.add_argument("--mode", default="hybrid", choices=["vector", "bm25", "hybrid"])
    an.add_argument("--service", help="회사로 거르기 (coupang, riotgames …). 없으면 질문에서 찾는다")
    an.add_argument("--llm", default="gpt-5.4-mini")
    an.add_argument("--no-rewrite", action="store_true", help="질문 재작성·분기 없이 원래 질문으로만 검색")
    an.add_argument("--no-route", action="store_true", help="질문 분기·DB 조회 없이 검색만")
    fl = rag_opts(add("flow", cmd_flow, docs=False, strategy=True))
    fl.add_argument("question")
    fl.add_argument("--service", help="회사로 거르기 (coupang, riotgames …). 추가 검색에서도 풀지 않는다")
    fl.add_argument("--mode", default="hybrid", choices=["vector", "bm25", "hybrid"])
    fl.add_argument("--llm", default="gpt-5.4-mini")
    fl.add_argument("--trace", action="store_true", help="단계별 판정 (Jev 확률, 검색어, 근거별 관련도)")
    fl.add_argument("--no-laws", action="store_true", help="약관이 인용한 법령 조문(legalize-kr)을 조회하지 않음")
    add("flow-diagram", cmd_flow_diagram, docs=False)
    wb = rag_opts(add("web", cmd_web, docs=False, strategy=True))
    wb.add_argument("--port", type=int, default=8767)
    wb.add_argument("--mode", default="hybrid", choices=["vector", "bm25", "hybrid"])
    wb.add_argument("--llm", default="gpt-5.4-mini")
    wb.add_argument("--no-laws", action="store_true", help="약관이 인용한 법령 조문(legalize-kr)을 조회하지 않음")
    lw = add("laws", cmd_laws, docs=False, strategy=True)
    lw.add_argument("--top", type=int, default=30, help="인용 많은 법령 몇 개를 보일지")
    lw.add_argument("--fetch", action="store_true", help="인용된 법령 파일(현행 판)을 legalize-kr 에서 미리 받아 캐시")
    lw.add_argument("--history", action="store_true", help="--fetch 때 변경 기록의 버전 날짜 기준 판도 받는다")
    add("db", cmd_db, docs=False, strategy=True)
    ft = add("ftc", cmd_ftc, docs=False, strategy=True)    # index 끝에도 돈다. 사례나 기준값만 바꿨을 때 따로
    ft.add_argument("--model", default=default_model(), help="임베딩 모델 (index 와 같아야 함)")
    ft.add_argument("--yes", action="store_true", help="예상 토큰이 WIMT_EMBED_MAX_TOKENS 를 넘어도 OpenAI 로 색인")
    tl = add("timeline", cmd_timeline, docs=False)
    tl.add_argument("path", help="문서 경로 (예: coupang/쿠팡이용약관.md)")
    tl.add_argument("clause", help="조항 (예: 제38조)")
    tl.add_argument("--chars", type=int, default=600)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except ModelError as e:                   # 임베딩 모델·색인 설정 문제는 설명만 보여 준다
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
