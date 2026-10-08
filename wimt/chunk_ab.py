"""Reproducible, local-only paired chunking experiment on a pinned Git snapshot.

python -m wimt.chunk_ab prepare
python -m wimt.chunk_ab run
All source-bearing artifacts and vectors remain under reports/ and .cache/.
"""
import argparse
import csv
import hashlib
import itertools
import json
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import repo
from .index import EMBED_MODEL, LocalEmbedder

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "chunk_ab"
CONFIG = {"A": {"limit": 1500, "overlap": 0}, "B": {"limit": 750, "overlap": 100}}
# Only line starts are structural; references inside prose cannot create articles.
SECTION = re.compile(r"(?m)^(?:#{1,6}\s+[^\n]+|제\s*\d+\s*조(?:\s*의\s*\d+)?\s*[.(（][^\n]*|\d+\.\s*\n\s*\n[^\n]+)")
PARAGRAPH = re.compile(r"\n\s*\n|(?=[①-⑳])|(?m:^\s*\[\d+\])")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunks(body, strategy):
    """Return exact source spans; never truncate long lines, including table rows.

    A packs paragraphs inside structural sections. B uses smaller paragraph
    windows with 100-character overlap, constrained to the same section.
    """
    conf = CONFIG[strategy]
    starts = sorted({0, len(body), *(m.start() for m in SECTION.finditer(body))})
    result = []
    for left, right in itertools.pairwise(starts):
        title = body[left:right].split("\n", 1)[0][:160]
        boundaries = [m.end() + left for m in PARAGRAPH.finditer(body[left:right])]
        pos = left
        while pos < right:
            end = min(pos + conf["limit"], right)
            if end < right:
                preferred = [x for x in boundaries if pos + conf["limit"] // 3 <= x <= end]
                if preferred:
                    end = preferred[-1]
                else:
                    # Prefer sentence/word boundaries; hard split is lossless fallback.
                    cut = max(body.rfind(". ", pos + conf["limit"] // 2, end),
                              body.rfind("\n", pos + conf["limit"] // 2, end))
                    if cut >= 0:
                        end = cut + 1
            if body[pos:end].strip():
                result.append({"start": pos, "end": end, "text": body[pos:end], "heading": title})
            if end == right:
                break
            pos = max(pos + 1, end - conf["overlap"])
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare(source=repo.DEFAULT_REPO, out=OUT):
    out.mkdir(parents=True, exist_ok=True)
    commit = repo.git(source, "rev-parse", "HEAD").strip()
    documents, records, statistics, probes = [], [], [], []
    # Read the same immutable commit, not potentially modified working files.
    paths = repo.documents(source)
    for path in paths:
        meta, body = repo.split_frontmatter(repo.git(source, "show", f"{commit}:{path}"))
        title = meta.get("제목") or Path(path).stem
        doc = {"path": path, "company": path.split("/")[0], "title": title,
               "body_hash": digest(body), "body": body}
        documents.append(doc)
        for strategy in CONFIG:
            parts = chunks(body, strategy)
            for i, part in enumerate(parts):
                records.append({**part, "id": f"{strategy}:{path}:{i}", "strategy": strategy,
                                "path": path, "company": doc["company"], "body_hash": doc["body_hash"],
                                "input": f"[{doc['company']} · {title}] {part['heading']}\n{part['text']}"})
            covered = bytearray(len(body))
            for part in parts:
                covered[part["start"]:part["end"]] = b"\1" * (part["end"] - part["start"])
            missing = sum(not covered[i] and not ch.isspace() for i, ch in enumerate(body))
            statistics.append({"company": doc["company"], "path": path, "strategy": strategy,
                               "chunks": len(parts), "max_chars": max((len(p["text"]) for p in parts), default=0),
                               "embedded_chars": sum(len(p["text"]) for p in parts), "missing_chars": missing})
            if missing:
                raise ValueError(f"Lost source characters: {path} {strategy}")
        # Diagnostic query only: explicitly NOT human-labelled relevance evidence.
        candidates = [m for m in re.finditer(r"[^\n]{40,180}", body) if not m.group().startswith(("#", "|"))]
        if candidates:
            m = candidates[len(candidates) // 2]
            probes.append({"id": f"probe:{path}", "path": path, "question": m.group(),
                           "start": m.start(), "end": m.end(), "body_hash": doc["body_hash"], "kind": "diagnostic"})
    write_json(out / "corpus.json", {"commit": commit, "config": CONFIG, "documents": documents, "chunks": records})
    write_json(out / "probes.json", probes)
    write_csv(out / "structure.csv", statistics, ["company", "path", "strategy", "chunks", "max_chars", "embedded_chars", "missing_chars"])
    # Separate draft file. Never overwrite reviewed questions supplied by a human.
    draft = [{**p, "question": "", "reviewed": "no"} for p in probes]
    write_csv(out / "questions.draft.csv", draft, ["id", "path", "question", "start", "end", "body_hash", "reviewed"])
    summary = {"commit": commit, "documents": len(documents), "companies": len({d['company'] for d in documents}),
               "chunks": {s: sum(r['strategy'] == s for r in records) for s in CONFIG},
               "missing_chars": sum(s['missing_chars'] for s in statistics)}
    write_json(out / "manifest.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def labelled_questions(corpus, out, sheet):
    """Exact source anchors make relevance independent of either strategy's IDs."""
    docs = {d["path"]: d for d in corpus["documents"]}
    questions, excluded = [], []
    if sheet.exists():
        with sheet.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("kind") != "current" or row.get("판정(채택/버림)") != "채택":
                    continue
                doc = docs.get(row["doc"])
                # Gold contains an entire clause; locate all its nonempty paragraphs.
                spans = []
                expected = 0
                if doc:
                    for part in re.split(r"\n\s*\n", row["text"].replace("\r\n", "\n")):
                        part = part.strip()
                        if len(part) < 15:
                            continue
                        expected += 1
                        start = doc["body"].find(part)
                        if start >= 0:
                            spans.append([start, start + len(part)])
                if not spans or len(spans) != expected:
                    excluded.append({"id": row["id"], "path": row["doc"], "reason": "gold text wholly or partly absent in pinned latest version",
                                     "matched_paragraphs": len(spans), "expected_paragraphs": expected})
                else:
                    questions.append({"id": f"gold:{row['id']}", "path": row["doc"], "question": row["question"],
                                      "spans": spans, "kind": "labelled"})
    custom = out / "questions.csv"
    if custom.exists():
        with custom.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("reviewed", "").lower() != "yes":
                    continue
                doc = docs.get(row["path"])
                start, end = int(row["start"]), int(row["end"])
                if not doc or row["body_hash"] != doc["body_hash"] or not 0 <= start < end <= len(doc["body"]) or not row["question"].strip():
                    raise ValueError(f"Invalid/stale reviewed question: {row['id']}")
                questions.append({"id": f"reviewed:{row['id']}", "path": row["path"], "question": row["question"],
                                  "spans": [[start, end]], "kind": "authored" if row.get('reviewer') == 'agent' else "labelled"})
    write_json(out / "excluded_gold.json", excluded)
    return questions


def cached_vectors(conn, embedder, texts, query=False):
    mode = "query" if query else "document"
    # Include settings that affect vectors, not just a model name.
    prefix = f"v1:{embedder.model}:{embedder.st.max_seq_length}:{embedder.st.prompts}:{mode}:"
    keys = [digest(prefix + t) for t in texts]
    unique = dict(zip(keys, texts))
    vectors = {}
    missing = []
    for key, value in unique.items():
        row = conn.execute("SELECT vector FROM vectors WHERE key=?", (key,)).fetchone()
        if row:
            vectors[key] = np.frombuffer(row[0], dtype=np.float32)
        else:
            missing.append((key, value))
    print(f"{mode}: cached={len(unique)-len(missing)} pending={len(missing)}", flush=True)
    start = time.monotonic()
    for i in range(0, len(missing), 32):
        batch = missing[i:i + 32]
        values = (embedder.query if query else embedder)([v for _, v in batch])
        for (key, _), value in zip(batch, values):
            vec = np.asarray(value, dtype=np.float32)
            if not np.isfinite(vec).all() or np.linalg.norm(vec) == 0:
                raise ValueError("Invalid embedding")
            vectors[key] = vec
            conn.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?)", (key, vec.tobytes()))
        conn.commit()
        print(f"{mode}: {min(i+32,len(missing))}/{len(missing)} elapsed={time.monotonic()-start:.1f}s", flush=True)
    return np.stack([vectors[k] for k in keys])


def fit_token_limit(corpus, tokenizer, limit):
    """Refine both strategies with the identical model limit, without lost text."""
    refined = []
    for record in corpus['chunks']:
        prefix = record['input'][:-len(record['text'])]
        text = record['text']
        if len(tokenizer(record['input'], truncation=False)['input_ids']) <= limit:
            refined.append(record)
            continue
        if len(tokenizer(prefix, truncation=False)['input_ids']) >= limit:
            raise ValueError('Metadata alone exceeds token limit')
        pos = 0
        while pos < len(text):
            low, high = 1, len(text) - pos
            while low < high:
                mid = (low + high + 1) // 2
                n = len(tokenizer(prefix + text[pos:pos+mid], truncation=False)['input_ids'])
                if n <= limit:
                    low = mid
                else:
                    high = mid - 1
            end = pos + low
            part = text[pos:end]
            if len(tokenizer(prefix + part, truncation=False)['input_ids']) > limit:
                raise ValueError('Unable to fit even one source character')
            refined.append({**record, 'id': f"{record['id']}:token:{pos}",
                            'start': record['start'] + pos, 'end': record['start'] + end,
                            'text': part, 'input': prefix + part})
            pos = end
    corpus['chunks'] = refined
    corpus['token_limit'] = limit
    return corpus


def evidence_coverage(records, spans, path):
    """Union coverage of gold spans; overlapping B chunks cannot double-count."""
    covered = total = 0
    for left, right in spans:
        total += right - left
        intervals = sorted((max(left, r["start"]), min(right, r["end"])) for r in records if r["path"] == path)
        cursor = left
        for a, b in intervals:
            a = max(a, cursor)
            if b > a:
                covered += b - a
                cursor = b
    return covered / total if total else 0.0


def evidence_hit(record, spans, path):
    # A boundary touching one character is not useful retrieval evidence.
    return record['path'] == path and any(
        min(record['end'], right) - max(record['start'], left) >= min(50, right-left)
        for left, right in spans)


def evaluate(corpus, matrix, questions, qvectors, out, topk=5, budget=3000):
    records = corpus["chunks"]
    rows = []
    for strategy in CONFIG:
        indices = [i for i, r in enumerate(records) if r["strategy"] == strategy]
        for q, vec in zip(questions, qvectors):
            company = q["path"].split("/")[0]
            for scope in ("company", "document"):
                eligible = [i for i in indices if (records[i]["company"] == company if scope == "company" else records[i]["path"] == q["path"])]
                scores = matrix[eligible] @ vec
                ranked = [records[eligible[int(j)]] for j in np.argsort(-scores, kind="stable")[:topk]]
                budgeted, remaining = [], budget
                for r in ranked:
                    # Same character budget for both: count duplicated overlap too.
                    n = min(len(r["text"]), remaining)
                    if n <= 0:
                        break
                    budgeted.append({**r, "end": r["start"] + n})
                    remaining -= n
                spans = q.get("spans") or [[q["start"], q["end"]]]
                hits = [evidence_hit(r, spans, q["path"]) for r in ranked]
                rows.append({"id": q["id"], "company": company, "path": q["path"], "strategy": strategy,
                             "scope": scope, "kind": q["kind"], "hit1": int(bool(hits) and hits[0]),
                             "hit5": int(any(hits)), "mrr5": next((1/(i+1) for i,h in enumerate(hits) if h), 0),
                             "coverage_at_3000_chars": evidence_coverage(budgeted, spans, q["path"])})
    fields = ["id", "company", "path", "strategy", "scope", "kind", "hit1", "hit5", "mrr5", "coverage_at_3000_chars"]
    write_csv(out / "results.csv", rows, fields)
    grouped = defaultdict(list)
    for r in rows:
        for level in ("company", "path"):
            grouped[(level, r[level], r["strategy"], r["scope"], r["kind"])].append(r)
    aggregates = []
    for (level, name, strategy, scope, kind), group in grouped.items():
        aggregates.append({"level": level, "name": name, "strategy": strategy, "scope": scope, "kind": kind,
                           "questions": len(group), **{f: sum(r[f] for r in group)/len(group) for f in fields[6:]}})
    write_json(out / "comparison.json", aggregates)
    return rows


def final_structure(corpus, out):
    grouped = defaultdict(list)
    for r in corpus['chunks']:
        grouped[(r['path'], r['strategy'])].append(r)
    rows = []
    for d in corpus['documents']:
        for strategy in CONFIG:
            records = grouped[(d['path'], strategy)]
            covered = bytearray(len(d['body']))
            for r in records:
                if r['text'] != d['body'][r['start']:r['end']]:
                    raise ValueError('Source span mismatch')
                covered[r['start']:r['end']] = b'\1' * (r['end'] - r['start'])
            missing = sum(not covered[i] and not ch.isspace() for i, ch in enumerate(d['body']))
            if missing:
                raise ValueError('Token refinement lost source text')
            rows.append({'company': d['company'], 'path': d['path'], 'strategy': strategy,
                         'chunks': len(records), 'max_chars': max((len(r['text']) for r in records), default=0),
                         'embedded_chars': sum(len(r['input']) for r in records), 'missing_chars': missing})
    write_csv(out / 'final_structure.csv', rows,
              ['company', 'path', 'strategy', 'chunks', 'max_chars', 'embedded_chars', 'missing_chars'])
    return rows


def comparison_tables(corpus, rows, out):
    """Include unassessed documents explicitly, instead of dropping them from reports."""
    groups = defaultdict(list)
    for r in rows:
        for level in ('company', 'path'):
            groups[(level, r[level], r['kind'], r['scope'], r['strategy'])].append(r)
    metrics = ['hit1', 'hit5', 'mrr5', 'coverage_at_3000_chars']
    fields = ['name', 'kind', 'scope', 'questions', 'status'] + [f'{s}_{m}' for s in CONFIG for m in metrics]
    for level, names in [('company', sorted({d['company'] for d in corpus['documents']})),
                         ('path', [d['path'] for d in corpus['documents']])]:
        table = []
        for name in names:
            for kind in ('labelled', 'authored', 'diagnostic'):
                for scope in ('company', 'document'):
                    count = len(groups[(level, name, kind, scope, 'A')])
                    row = {"name": name, "kind": kind, "scope": scope, "questions": count,
                           "status": 'evaluated' if count else 'unassessed'}
                    for strategy in CONFIG:
                        group = groups[(level, name, kind, scope, strategy)]
                        if len(group) != count:
                            raise ValueError('Unpaired evaluation questions')
                        row.update({f'{strategy}_{m}': sum(float(r[m]) for r in group)/len(group) if group else '' for m in metrics})
                    table.append(row)
        write_csv(out / f'{level}_comparison.csv', table, fields)
    by_company = []
    for company in sorted({d['company'] for d in corpus['documents']}):
        n = sum(d['company'] == company for d in corpus['documents'])
        counts = {s: sum(r['company'] == company and r['strategy'] == s for r in corpus['chunks']) for s in CONFIG}
        by_company.append(f"| {company} | {n} | {counts['A']} | {counts['B']} |")
    report = ['# 로컬 임베딩·청킹 A/B 결과', '', f"고정 Git 커밋: `{corpus['commit']}`. 최신 약관만 평가.", '',
              '| 기업 | 약관 수 | A 조각 | B 조각 |', '|---|---:|---:|---:|', *by_company, '',
              '각 약관 모두 A와 B로 임베딩했습니다. 본문 누락 및 토큰 상한 검사는 final_structure.csv에 기록했습니다.', '',
              '## 결과를 보는 방법', '',
              '- company_comparison.csv: 기업별 A/B를 나란히 비교합니다.',
              '- path_comparison.csv: 약관별 A/B를 나란히 비교합니다. 정답 질문이 없으면 unassessed로 표시합니다.',
              '- labelled: 기존 사용자 검수 질문. authored: 에이전트 작성 질문. diagnostic: 원문 재검색 기능 점검.',
              '- scope=company는 기업 전체 약관에서, scope=document는 특정 약관 안에서 검색합니다.',
              '- A/B에 같은 질문, 모델, 상위 5개, 3,000자 문맥 예산을 사용합니다.',
              '- SLA 질문 다수는 같은 질문 틀을 사용하므로 다양한 실제 질문의 정확도로 해석하면 안 됩니다.',
              '- 검수되지 않은 신규 질문과 기능 점검을 근거로 운영 전략의 승자를 확정하지 않습니다.', '']
    for kind, label in [('labelled', '사용자 검수 질문'), ('authored', '에이전트 작성 질문'), ('diagnostic', '기능 점검')]:
        report += [f'## {label}: 기업 범위 검색', '', '| 전략 | 질문 수 | hit@1 | hit@5 | MRR@5 | 근거 포함률 |', '|---|---:|---:|---:|---:|---:|']
        for strategy in CONFIG:
            selected = [r for r in rows if r['kind'] == kind and r['scope'] == 'company' and r['strategy'] == strategy]
            if selected:
                means = [sum(float(r[m]) for r in selected)/len(selected) for m in metrics]
                report.append(f"| {strategy} | {len(selected)} | " + ' | '.join(f'{m:.3f}' for m in means) + ' |')
        report.append('')
    (out / 'summary.md').write_text('\n'.join(report), encoding='utf-8')


def run(out=OUT, batch=4):

    corpus = json.loads((out / "corpus.json").read_text(encoding="utf-8"))
    if corpus["config"] != CONFIG:
        raise ValueError("Strategy changed; run prepare again")
    questions = labelled_questions(corpus, out, ROOT / "reports" / "qa_sheet.csv")
    questions += json.loads((out / "probes.json").read_text(encoding="utf-8"))
    LocalEmbedder.BATCH = batch
    embedder = LocalEmbedder(EMBED_MODEL)  # Explicitly local; ignore .env paid-model defaults.
    print(f"model ready: {embedder.model} device={embedder.device}", flush=True)
    fit_token_limit(corpus, embedder.st.tokenizer, embedder.st.max_seq_length)
    write_json(out / 'corpus.json', corpus)
    final_structure(corpus, out)
    # Guard query truncation too; evaluation questions must be fully encoded.
    prompt = embedder.st.prompts.get(embedder.query_prompt, '') if embedder.query_prompt else ''
    if any(len(embedder.st.tokenizer(prompt + q['question'], truncation=False)['input_ids']) > embedder.st.max_seq_length for q in questions):
        raise ValueError('An evaluation question exceeds the model token limit')
    cache = ROOT / ".cache" / "chunk_ab_vectors.sqlite"
    cache.parent.mkdir(exist_ok=True, parents=True)
    start = time.monotonic()
    with sqlite3.connect(cache) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        matrix = cached_vectors(conn, embedder, [r["input"] for r in corpus["chunks"]])
        qvectors = cached_vectors(conn, embedder, [q["question"] for q in questions], query=True)
    np.save(out / "vectors.npy", matrix)
    rows = evaluate(corpus, matrix, questions, qvectors, out)
    comparison_tables(corpus, rows, out)
    summary = {"commit": corpus["commit"], "model": EMBED_MODEL, "device": embedder.device,
               "corpus_hash": digest((out / 'corpus.json').read_text(encoding='utf-8')),
               "batch_size": batch,
               "chunks_by_strategy": {s: sum(r['strategy'] == s for r in corpus['chunks']) for s in CONFIG},
               "documents": len(corpus["documents"]), "vectors": len(matrix), "dimensions": matrix.shape[1],
               "labelled_questions": sum(q["kind"] == "labelled" for q in questions),
               "authored_questions": sum(q["kind"] == "authored" for q in questions),
               "diagnostic_questions": sum(q["kind"] == "diagnostic" for q in questions),
               "elapsed_seconds": round(time.monotonic()-start, 1), "result_rows": len(rows),
               "note": "Diagnostic source-text queries test plumbing only, not retrieval quality. Latest snapshot only."}
    write_json(out / "run.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def search(question, strategy, company=None, path=None, out=OUT, batch=4):
    """Use the experimental local vectors without changing the production index."""

    if not question.strip() or (not company and not path):
        raise ValueError('Provide a question and --company or --path')
    manifest = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    text = (out / 'corpus.json').read_text(encoding='utf-8')
    if digest(text) != manifest['corpus_hash']:
        raise ValueError('Corpus changed since embedding; run again before searching')
    corpus = json.loads(text)
    matrix = np.load(out / 'vectors.npy', mmap_mode='r')
    if len(matrix) != len(corpus['chunks']):
        raise ValueError('Vector count does not match corpus')
    indices = [i for i, r in enumerate(corpus['chunks']) if r['strategy'] == strategy
               and (not company or r['company'] == company) and (not path or r['path'] == path)]
    if not indices:
        raise ValueError('No documents match these filters')
    LocalEmbedder.BATCH = batch
    embedder = LocalEmbedder(manifest['model'])
    vec = np.asarray(embedder.query([question])[0], dtype=np.float32)
    scores = matrix[indices] @ vec
    result = [{**{k: corpus['chunks'][indices[int(j)]][k] for k in ('id', 'path', 'start', 'end', 'text')},
               'score': float(scores[int(j)])} for j in np.argsort(-scores, kind='stable')[:5]]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "search"])
    parser.add_argument("--repo", type=Path, default=repo.DEFAULT_REPO)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument('--question', default='')
    parser.add_argument('--strategy', choices=list(CONFIG), default='A')
    parser.add_argument('--company')
    parser.add_argument('--path')
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.repo, args.out)
    elif args.command == 'run':
        run(args.out, args.batch)
    else:
        search(args.question, args.strategy, args.company, args.path, args.out, args.batch)


if __name__ == "__main__":
    main()
