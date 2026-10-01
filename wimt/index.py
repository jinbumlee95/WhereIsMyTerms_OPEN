"""벡터 DB 색인: 최신 버전의 조항을 임베딩해 Chroma 에 넣고, 메타데이터 필터와 함께 의미 검색한다.

- 색인 단위: 조항 하나. 긴 조항(PIECE_CHARS 초과)은 항 경계에서, 항 하나가 너무 길면(큰 표) 줄 경계에서 여러 조각으로 자른다.
  표를 자르면 각 조각 앞에 표 머리줄을 다시 붙인다.
- 연결 정보: 조각마다 group(원래 조항), piece/pieces(몇 번째 조각), prev/next(앞뒤 조각 id), refs(본문이 가리키는 같은 문서의 다른 조).
  검색에서 조각 하나가 걸리면 같은 조항의 조각을 모두 가져와 조항 전체로 돌려주고, 필요하면 refs 로 가리킨 조도 함께 가져온다.
- 임베딩: 기본은 로컬 한국어 모델 dragonkue/snowflake-arctic-embed-l-v2.0-ko (OpenAI small 보다 recall@1 0.42→0.62,
  README 비교표). OpenAI 는 --model text-embedding-3-small. `[문서 제목] 조항 식별자 조 제목 (조각 k/n)` + 조각 본문.
- 메타데이터: 문서 층(서비스·문서 종류·경로) + 버전(버전일자) + 조항(식별자·제목) + 유불리(조각 지수·조항 지수·채점기) + 연결 정보
- 증분: 조각 해시가 같으면 다시 임베딩하지 않고 메타데이터만 갱신한다. 최신 버전에서 사라진 조각은 지운다.
"""
import hashlib
import importlib.util
import os
import re
import threading
from pathlib import Path

from .pipeline import UNFAVORABLE
from .score import is_list  # noqa: F401  (조각 판정과 채점 제외가 같은 기준을 쓴다)
from .services import doc_label, platform

EMBED_MODEL = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"   # 기본 (로컬, requirements-local.txt)
OPENAI_EMBED_MODEL = "text-embedding-3-small"                # 처음 쓰던 모델. 컬렉션 이름도 그때 것을 그대로 쓴다
OPENAI_MODELS = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}   # 100만 토큰당 달러
MODEL_ENV = "WIMT_EMBED_MODEL"             # PC 마다 기본 임베딩 모델을 바꾼다 (.env 가능). GPU 없는 PC 는 OpenAI
MAX_TOKENS_ENV = "WIMT_EMBED_MAX_TOKENS"   # OpenAI 색인 한 번에 허용할 예상 토큰 (넘으면 --yes 없이는 멈춘다)
MAX_TOKENS = 200_000
LIST_PIECES = 3             # 이름 나열·표 조각은 조항마다 앞 3개만 벡터로 (KT 수탁사 목록 15만 자 = 99조각). 나머지는 BM25 로만
EMBED_CHARS = 6000          # 임베딩에 넣을 최대 글자 수 (조각은 PIECE_CHARS 로 잘리므로 안전장치)
EMBED_BATCH = 100
PIECE_CHARS = 1500          # 이보다 긴 조항은 여러 조각으로 나눈다
EXPAND_ALL = 8              # 조각이 이 이하인 조항은 검색에 걸리면 전체를, 더 많으면(큰 표 등) 걸린 조각과 앞뒤 조각만 돌려준다

# 본문 속 다른 조 참조: "제10조", "제 3 조의2", "제5조 제2항" -> 제10조, 제3조의2, 제5조
_REF = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def _collection(db_dir: Path, name: str, model: str):
    import chromadb

    client = chromadb.PersistentClient(path=str(db_dir))
    # 임베딩은 직접 계산해 넣으므로 Chroma 의 기본 임베딩 함수는 쓰지 않는다
    return client.get_or_create_collection(name, embedding_function=None,
                                           metadata={"hnsw:space": "cosine", "embed_model": model})


def collection_name(kind: str, strategy: str, model: str) -> str:
    """컬렉션은 모델마다 따로 둔다 (차원이 달라 섞을 수 없다). OpenAI small 은 처음 만든 이름(clauses_article)을 그대로 쓴다."""
    if model == OPENAI_EMBED_MODEL:
        return f"{kind}_{strategy}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", model.split("/")[-1]).strip("-").lower()[:40]
    return f"{kind}_{strategy}__{slug}"


class ModelError(RuntimeError):
    """임베딩 모델·색인 설정 문제. CLI 는 메시지만 보여 주고 끝낸다."""


def default_model() -> str:
    """기본 임베딩 모델: 환경 변수 WIMT_EMBED_MODEL (없으면 EMBED_MODEL). .env 를 읽은 뒤에 불러야 한다."""
    value = os.environ.get(MODEL_ENV)
    return EMBED_MODEL if value is None else value.strip()


def model_source(model: str) -> str:
    if os.environ.get(MODEL_ENV, "").strip() == model:
        return MODEL_ENV
    return "기본값" if model == EMBED_MODEL else "--model"


def check_model(model: str):
    """모델을 쓸 수 있는지: 이름, OpenAI 키, 로컬 모델 패키지. 문제가 있으면 ModelError."""
    if not model:
        raise ModelError(f"임베딩 모델 이름이 비어 있습니다. {MODEL_ENV} 값을 확인하세요.")
    if model.startswith("text-embedding-"):
        if model not in OPENAI_MODELS:
            raise ModelError(f"모르는 OpenAI 임베딩 모델: {model} ({model_source(model)}). "
                             f"쓸 수 있는 것: {', '.join(OPENAI_MODELS)}")
        if not os.environ.get("OPENAI_API_KEY"):
            raise ModelError(f"{model} 을 쓰려면 OPENAI_API_KEY 가 필요합니다 (.env).")
    elif importlib.util.find_spec("sentence_transformers") is None:
        raise ModelError(f"로컬 임베딩 모델 {model} ({model_source(model)}) 을 쓰려면 requirements-local.txt 가 필요합니다 "
                         f"(torch 2.5.1 이라 Python 3.12 이하). 이 PC 에서 OpenAI 를 쓰려면 .env 에 "
                         f"{MODEL_ENV}=text-embedding-3-small 처럼 적으세요.")
    elif not has_directml():
        raise ModelError(f"로컬 임베딩 모델 {model} ({model_source(model)}) 은 GPU(DirectML)로만 돌립니다. "
                         f"onnxruntime-directml 이 없거나 GPU 를 못 찾았습니다 (CPU 판 onnxruntime 이 덮어썼다면 "
                         f"지우고 onnxruntime-directml 을 다시 설치). 이 PC 에서 OpenAI 를 쓰려면 .env 에 "
                         f"{MODEL_ENV}=text-embedding-3-small 처럼 적으세요.")


def has_directml() -> bool:
    """ONNX Runtime 에 DirectML(GPU) 실행 장치가 있는가. 로컬 임베딩은 CPU 로 돌리지 않는다."""
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    return "DmlExecutionProvider" in ort.get_available_providers()


def check_index(db_dir: Path, kind: str, strategy: str, model: str, ids) -> None:
    """검색 전에 색인 확인: 이 모델로 만든 컬렉션이 있고, 지금 scan 결과의 조각(ids)과 같은가.
    없으면 Chroma 가 빈 컬렉션을 새로 만들어 조용히 빈 결과를 돌려주고, 오래됐으면 새 조각이 조용히 빠진다."""
    import chromadb

    name = collection_name(kind, strategy, model)
    client = chromadb.PersistentClient(path=str(db_dir))
    if name not in {c.name for c in client.list_collections()}:
        raise ModelError(f"{model} 로 만든 색인({name})이 없습니다. 먼저 python -m wimt index --model {model} "
                         f"--strategy {strategy} 를 실행하세요.")
    col = _collection(db_dir, name, model)
    built_with = (col.metadata or {}).get("embed_model")
    if built_with and built_with != model:
        raise ModelError(f"색인({name})은 {built_with} 로 만들었는데 지금 모델은 {model} 입니다.")
    have, want = set(col.get(include=[])["ids"]), set(ids)
    missing, extra = len(want - have), len(have - want)
    if missing or extra:
        raise ModelError(f"색인({name})이 scan 결과와 다릅니다 (없는 조각 {missing}개, 지난 조각 {extra}개). "
                         f"python -m wimt index --model {model} --strategy {strategy} 로 갱신하세요.")


def make_embedder(model: str = EMBED_MODEL):
    """OpenAI 모델 이름(text-embedding-…)이면 API, 아니면 Hugging Face 의 sentence-transformers 모델을 로컬에서."""
    check_model(model)
    return Embedder(model) if model.startswith("text-embedding-") else LocalEmbedder(model)


def _allow_onnxruntime_directml(version: str):
    """optimum 은 설치 패키지 이름 목록으로 ONNX Runtime 을 찾는데, 그 목록에 onnxruntime-directml 이 없다.
    모듈(onnxruntime)은 있으니 optimum 의 판단만 바로잡는다 (site-packages 는 건드리지 않는다)."""
    import optimum.utils.import_utils as oiu
    if not oiu._onnxruntime_available:
        oiu._onnxruntime_available, oiu._onnxruntime_version = True, version


class LocalEmbedder:
    """로컬 임베딩 (예: dragonkue/snowflake-arctic-embed-l-v2.0-ko).

    GPU 전용: Windows 에서 ONNX Runtime + DirectML 로 AMD·Intel·NVIDIA GPU 를 쓴다 (onnxruntime-directml).
    DirectML 이 없으면 CPU 로 넘어가지 않고 ModelError (CPU 는 색인 한 번에 약 55분). 질문은 모델이 정한 query 프롬프트를 붙여 인코딩한다 (arctic-embed 는 "query: ").
    """
    MAX_TOKENS = 1024        # 조각은 1,500자 이하라 충분하고, 긴 입력의 메모리·시간을 막는다
    BATCH = 16

    CACHE = Path(__file__).resolve().parents[1] / ".cache" / "models"     # ONNX 로 바꾼 모델 (처음 한 번만 변환)

    def __init__(self, model: str):
        check_model(model)                   # DirectML 이 없으면 여기서 멈춘다
        import onnxruntime as ort
        from sentence_transformers import SentenceTransformer

        self.model, self.tokens, self.device = model, 0, "directml"
        self.lock = threading.Lock()
        _allow_onnxruntime_directml(ort.__version__)
        local = self.CACHE / re.sub(r"[^A-Za-z0-9]+", "-", model).strip("-")
        kw = {"backend": "onnx", "model_kwargs": {"provider": "DmlExecutionProvider"}}
        if (local / "onnx" / "model.onnx").exists():
            self.st = SentenceTransformer(str(local), **kw)
        else:
            self.st = SentenceTransformer(model, **kw)
            self.st.save_pretrained(str(local))
        self.st.max_seq_length = min(self.st.max_seq_length or self.MAX_TOKENS, self.MAX_TOKENS)
        self.query_prompt = "query" if "query" in (self.st.prompts or {}) else None

    BUCKETS = (128, 256, 512, 768, 1024)

    def _encode(self, texts: list[str], prompt: str = "") -> list[list[float]]:
        with self.lock:                      # GPU(DirectML) 세션은 동시 호출에 안전하지 않다 (웹은 여러 스레드가 함께 쓴다)
            return self._encode_locked(texts, prompt)

    def _encode_locked(self, texts: list[str], prompt: str) -> list[list[float]]:
        texts = [prompt + t for t in texts]
        lens = [len(self.st.tokenizer(t, truncation=True, max_length=self.st.max_seq_length)["input_ids"]) for t in texts]
        self.tokens += sum(lens)
        # DirectML 은 입력 모양이 바뀔 때마다 그래프를 다시 준비해서(수십 초) 느리다.
        # 길이를 몇 개 구간으로, 배치 크기를 고정해 모양 수를 줄인다 (구간마다 한 번만 준비).
        import torch
        order = sorted(range(len(texts)), key=lambda i: lens[i])
        out: list = [None] * len(texts)
        for b in range(0, len(order), self.BATCH):
            idx = order[b:b + self.BATCH]
            size = next((s for s in self.BUCKETS if s >= max(lens[i] for i in idx)), self.BUCKETS[-1])
            batch = [texts[i] for i in idx] + [""] * (self.BATCH - len(idx))       # 빈 칸 채우기 (결과는 버린다)
            enc = self.st.tokenizer(batch, padding="max_length", truncation=True, max_length=size, return_tensors="pt")
            with torch.no_grad():
                emb = self.st(dict(enc))["sentence_embedding"]
            emb = torch.nn.functional.normalize(emb, dim=-1)
            for j, i in enumerate(idx):
                out[i] = emb[j].tolist()
        return out

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def query(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, self.st.prompts.get(self.query_prompt, "") if self.query_prompt else "")


class Embedder:
    def __init__(self, model: str = OPENAI_EMBED_MODEL):
        from openai import OpenAI

        self.client, self.model = OpenAI(max_retries=8), model   # 분당 토큰 한도(429)는 기다렸다 재시도
        self.tokens = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out = []
        for i in range(0, len(texts), EMBED_BATCH):
            r = self.client.embeddings.create(model=self.model, input=texts[i:i + EMBED_BATCH])
            self.tokens += r.usage.total_tokens
            out += [d.embedding for d in r.data]
        return out

    query = __call__            # OpenAI 임베딩은 질문과 문서를 구분하지 않는다


# ---------------------------------------------------------------------------
# 조각 나누기와 연결 정보
# ---------------------------------------------------------------------------
def _split_lines(text: str, limit: int) -> list[str]:
    """항 하나가 너무 길 때 줄 경계에서 자른다. 표 중간에서 자르면 다음 조각 앞에 표 머리줄(머리 + 구분줄)을 붙인다."""
    lines, out, buf, header = text.split("\n"), [], [], None
    for i, line in enumerate(lines):
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1]):
            header = [line, lines[i + 1]]              # 표 시작
        elif not line.startswith("|"):
            header = None                              # 표 끝
        if buf and len("\n".join(buf)) + len(line) + 1 > limit:
            out.append("\n".join(buf))
            buf = list(header) if (header and line.startswith("|") and line not in header) else []
        buf.append(line[:limit])                       # 줄 하나가 limit 보다 길면 그 줄은 잘라 넣는다
    if buf:
        out.append("\n".join(buf))
    return out


def pieces(c: dict, limit: int = PIECE_CHARS) -> list[dict]:
    """조항 하나를 [{text, favor_score, parts}] 조각으로. 항 경계를 우선하고, 조각 지수는 조각 안 항들 중 가장 불리한 값."""
    parts = c.get("parts") or [{"clause_id": c["clause_id"], "text": c["text"], "favor_score": c["favor_score"]}]
    if len(c["text"]) <= limit:
        return [{"text": c["text"], "favor_score": c["favor_score"], "parts": [p["clause_id"] for p in parts]}]
    out: list[dict] = []
    for p in parts:
        chunks = [p["text"]] if len(p["text"]) <= limit else _split_lines(p["text"], limit)
        for k, t in enumerate(chunks):
            last = out[-1] if out else None
            # 짧은 항은 앞 조각에 이어 붙인다 (같은 항을 줄 단위로 자른 조각은 합치지 않는다)
            if last and k == 0 and len(chunks) == 1 and len(last["text"]) + len(t) + 2 <= limit and not last.get("split"):
                last["text"] += "\n\n" + t
                last["favor_score"] = min(last["favor_score"], p["favor_score"])
                last["parts"].append(p["clause_id"])
            else:
                out.append({"text": t, "favor_score": p["favor_score"], "parts": [p["clause_id"]], "split": len(chunks) > 1})
    for o in out:
        o.pop("split", None)
    return out


def refs(text: str, self_article: str, known: set[str]) -> list[str]:
    """본문이 가리키는 같은 문서의 다른 조 (자기 자신·없는 조 제외, 나온 순서대로)."""
    seen = []
    for m in _REF.finditer(text):
        a = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
        if a != self_article and a in known and a not in seen:
            seen.append(a)
    return seen


def group_id(c: dict) -> str:
    return f"{c['path']}::{c['clause_id']}"


def entries(clauses: list[dict], limit: int = PIECE_CHARS) -> list[dict]:
    """색인할 조각 목록: {id, text, embed, meta}. 조각끼리의 연결 정보를 메타데이터에 담는다."""
    by_doc: dict[str, set[str]] = {}
    for c in clauses:
        by_doc.setdefault(c["path"], set()).add(c["clause_id"])
    out = []
    for c in clauses:
        g = group_id(c)
        ps = pieces(c, limit)
        ids = [g if len(ps) == 1 else f"{g}#p{k + 1}" for k in range(len(ps))]
        article = c["clause_id"].split("#")[0]
        rs = refs(c["text"], article, by_doc[c["path"]])
        lists = 0
        for k, (pid, p) in enumerate(zip(ids, ps)):
            lists += is_list(p["text"])
            # 머리말에 서비스 이름·플랫폼을 넣어 "발로란트", "스팀" 같은 말로도 찾히게 한다
            head = f"[{doc_label(c)}] {c['clause_id']} {c.get('title') or ''}".strip()
            if len(ps) > 1:
                head += f" (조각 {k + 1}/{len(ps)})"
            embed = (head + "\n" + p["text"])[:EMBED_CHARS]
            meta = {
                "service": c["service"], "company": c["service"].split("/")[0], "doc_type": c["doc_type"],
                "path": c["path"], "doc_title": c.get("doc_title") or "", "version_date": c.get("version_date") or "",
                "clause_id": c["clause_id"], "title": c.get("title") or "",
                "platform": platform(c["path"]), "lineage": c.get("lineage") or "",
                "favor_score": float(p["favor_score"]), "clause_score": float(c["favor_score"]),
                "favor_confidence": float(c["favor_confidence"]),
                "is_unfavorable": p["favor_score"] <= UNFAVORABLE, "scorer": c.get("scorer") or "",
                # 연결 정보 (Chroma 메타데이터는 목록을 못 담아서 문자열로)
                "group": g, "piece": k + 1, "pieces": len(ps),
                "prev": ids[k - 1] if k > 0 else "", "next": ids[k + 1] if k + 1 < len(ps) else "",
                "parts": ",".join(p["parts"]), "refs": ",".join(rs),
                # 벡터로 넣을 조각인가: 목록 조각은 조항마다 앞 LIST_PIECES 개만 (나머지는 BM25·조항 복원에서만 쓴다)
                "vector": not (is_list(p["text"]) and lists > LIST_PIECES),
            }
            meta["embed_hash"] = hashlib.sha256(embed.encode("utf-8")).hexdigest()[:16]
            out.append({"id": pid, "text": p["text"], "embed": embed, "meta": meta})
    return out


# ---------------------------------------------------------------------------
# 변경 이력 조각: scan 레코드(추가·삭제·수정) 하나 = 조각 하나
# ---------------------------------------------------------------------------
CHANGE_KIND = {"added": "추가", "removed": "삭제", "modified": "수정"}
CHANGE_DOC_CHARS = 4000
CHANGE_EMBED_CHARS = 2000   # 표가 많은 변경은 글자당 토큰이 많아 임베딩 입력 한도(8192 토큰)를 넘을 수 있다


def change_text(r: dict) -> str:
    """변경 레코드를 사람이 읽는 변경 설명으로. 수정이면 바뀐 항의 전·후만 보여 준다."""
    head = (f"[{doc_label(r)}] {r['clause_id']} {r.get('title') or ''} · "
            f"{r['version_date']} 개정 ({CHANGE_KIND[r['change_type']]})").strip()
    if r.get("effective_date") and r["effective_date"] != r["version_date"]:
        head += f" · 시행 {r['effective_date']}"
    if r["change_type"] == "added":
        body = "추가된 조항:\n" + r["text"]
    elif r["change_type"] == "removed":
        body = "삭제된 조항:\n" + r["text"]
    else:
        lines = []
        for u in r.get("unit_changes") or []:
            kind = CHANGE_KIND[u["change_type"]]
            if u["change_type"] == "modified":
                lines.append(f"- {u['clause_id']} {kind}\n  전: {u.get('old_text') or ''}\n  후: {u['text']}")
            else:
                lines.append(f"- {u['clause_id']} {kind}: {u['text']}")
        body = "\n".join(lines) or f"전:\n{r.get('old_text') or ''}\n후:\n{r['text']}"
    return (head + "\n" + body)[:CHANGE_DOC_CHARS]


def change_id(r: dict) -> str:
    return f"{r['path']}::{r['clause_id']}::{r['version_date']}::{r['change_type']}"


def change_entries(records: list[dict]) -> list[dict]:
    """최초 버전(initial)을 뺀 변경 레코드를 색인 조각으로."""
    out, seen = [], {}
    for r in records:
        if r["change_type"] == "initial":
            continue
        cid = change_id(r)
        seen[cid] = seen.get(cid, 0) + 1
        if seen[cid] > 1:
            cid += f"#{seen[cid]}"
        text = change_text(r)
        meta = {
            "service": r["service"], "company": r["service"].split("/")[0], "doc_type": r["doc_type"],
            "path": r["path"], "doc_title": r.get("doc_title") or "", "clause_id": r["clause_id"],
            "title": r.get("title") or "", "version_date": r["version_date"],
            "effective_date": r.get("effective_date") or "", "commit": r["commit"], "change_type": r["change_type"],
            "lineage": r.get("lineage") or "", "platform": platform(r["path"]), "group": cid, "piece": 1, "pieces": 1, "refs": "",
            "embed_hash": hashlib.sha256(text[:CHANGE_EMBED_CHARS].encode("utf-8")).hexdigest()[:16],
            "scorer": r.get("scorer") or "",
        }
        # 화면 표시용 유불리 지수 (바뀐 뒤, 바뀌기 전). Chroma 메타데이터는 None 을 못 담으므로 있을 때만
        if r.get("favor_score") is not None and r["change_type"] != "removed":
            meta["favor_score"] = float(r["favor_score"])
            if r.get("score_delta") is not None:
                meta["old_score"] = float(r["favor_score"]) - float(r["score_delta"])
        out.append({"id": cid, "text": text, "embed": text[:CHANGE_EMBED_CHARS], "meta": meta})
    return out


# ---------------------------------------------------------------------------
# 색인
# ---------------------------------------------------------------------------
def vector_ids(es: list[dict]) -> list[str]:
    """컬렉션에 들어가야 하는 조각 id (목록 조각의 뒷부분은 빠진다)."""
    return [e["id"] for e in es if e["meta"].get("vector", True)]


def _stored(col) -> dict:
    existing = col.get(include=["metadatas"]) if col.count() else {"ids": [], "metadatas": []}
    return {i: (m or {}).get("embed_hash") for i, m in zip(existing["ids"], existing["metadatas"])}


def pending(es: list[dict], db_dir: Path, name: str, model: str) -> list[dict]:
    """색인하면 새로 임베딩할 조각 (임베딩 텍스트가 바뀌었거나 없는 것). 비용 확인용: 임베딩은 하지 않는다."""
    have = _stored(_collection(db_dir, name, model))
    return [e for e in es if e["meta"].get("vector", True) and have.get(e["id"]) != e["meta"]["embed_hash"]]


def estimate_tokens(es: list[dict]) -> int:
    """예상 임베딩 토큰. 이 코퍼스는 OpenAI 토큰이 글자 수의 0.94~0.96 배였으므로 글자 수로 넉넉히 잡는다."""
    return sum(len(e["embed"]) for e in es)


def check_budget(model: str, tokens: int, yes: bool = False) -> str:
    """OpenAI 색인 전 비용 확인. 예상 토큰이 한도(WIMT_EMBED_MAX_TOKENS, 기본 20만)를 넘으면 --yes 없이는 멈춘다.
    돌려주는 값은 사람이 읽을 예상 비용 설명."""
    if model not in OPENAI_MODELS:
        return f"예상 토큰 {tokens:,} (로컬 모델, 비용 없음)"
    note = f"예상 토큰 {tokens:,} (약 ${tokens / 1e6 * OPENAI_MODELS[model]:.2f}, {model})"
    raw = os.environ.get(MAX_TOKENS_ENV, "").strip()
    try:
        limit = int(raw.replace(",", "").replace("_", "")) if raw else MAX_TOKENS
    except ValueError:
        raise ModelError(f"{MAX_TOKENS_ENV} 는 정수여야 합니다: {raw!r}") from None
    if tokens > limit and not yes:
        raise ModelError(f"{note} 이 한도 {limit:,} 를 넘습니다. 정말 색인하려면 --yes 를 붙이세요 "
                         f"(한도는 {MAX_TOKENS_ENV}).")
    return note


def upsert(es: list[dict], db_dir: Path, name: str, embedder: Embedder) -> dict:
    """조각 목록을 컬렉션에 맞춘다. 임베딩 텍스트가 같으면 다시 임베딩하지 않고 메타데이터만 갱신, 없어진 조각은 지운다.
    벡터로 넣지 않는 조각(목록 조각의 뒷부분)은 컬렉션에서 뺀다."""
    col = _collection(db_dir, name, embedder.model)
    es = [e for e in es if e["meta"].get("vector", True)]
    have = _stored(col)
    todo = [e for e in es if have.get(e["id"]) != e["meta"]["embed_hash"]]
    same = [e for e in es if e["id"] in have and have[e["id"]] == e["meta"]["embed_hash"]]
    ids = {e["id"] for e in es}
    gone = [i for i in have if i not in ids]
    for k in range(0, len(todo), 500):
        batch = todo[k:k + 500]
        col.upsert(ids=[e["id"] for e in batch], embeddings=embedder([e["embed"] for e in batch]),
                   documents=[e["text"] for e in batch], metadatas=[e["meta"] for e in batch])
    for k in range(0, len(same), 1000):
        batch = same[k:k + 1000]
        col.update(ids=[e["id"] for e in batch], metadatas=[e["meta"] for e in batch])
    if gone:
        col.delete(ids=gone)
    return {"collection": col.name, "total": col.count(), "embedded": len(todo), "metadata_only": len(same),
            "deleted": len(gone)}


def build(clauses: list[dict], db_dir: Path, strategy: str, embedder: Embedder) -> dict:
    es = entries(clauses)
    stats = upsert(es, db_dir, collection_name("clauses", strategy, embedder.model), embedder)
    return {**stats, "clauses": len(clauses), "split": sum(1 for e in es if e["meta"]["piece"] == 2),
            "tokens": embedder.tokens}


def build_changes(records: list[dict], db_dir: Path, strategy: str, embedder: Embedder) -> dict:
    before = embedder.tokens
    stats = upsert(change_entries(records), db_dir, collection_name("changes", strategy, embedder.model), embedder)
    return {**stats, "tokens": embedder.tokens - before}


# ---------------------------------------------------------------------------
# 검색: 걸린 조각 -> 조항 전체 (+ 참조한 조)
# ---------------------------------------------------------------------------
def expand(hits: list[dict], fetch, with_refs: bool = False, expand_all: int = EXPAND_ALL) -> list[dict]:
    """hits: [{id, text, meta, similarity}] (유사도 높은 순). fetch(where) -> [{id, text, meta}].

    같은 조항의 조각은 하나로 묶어 조항 전체 텍스트를 조각 순서대로 이어 붙이고, 걸린 조각 번호를 남긴다.
    조각이 expand_all 개보다 많으면 걸린 조각과 그 앞뒤(prev/next) 조각만 붙인다 (생략된 곳은 "…").
    with_refs 면 조항이 가리키는 같은 문서의 다른 조도 related 로 붙인다.
    """
    groups: dict[str, dict] = {}
    for h in hits:
        g = h["meta"]["group"]
        if g not in groups:
            groups[g] = {**h["meta"], "similarity": h["similarity"], "hit_pieces": []}
        groups[g]["hit_pieces"].append(h["meta"]["piece"])
    split = [g for g, v in groups.items() if v["pieces"] > 1]
    got: dict[str, list[dict]] = {h["meta"]["group"]: [h] for h in hits if h["meta"]["pieces"] == 1}
    if split:
        for r in fetch({"group": {"$in": split}}):
            got.setdefault(r["meta"]["group"], []).append(r)
    out = []
    for g, v in groups.items():
        ps = sorted(got.get(g, []), key=lambda r: r["meta"]["piece"])
        hit = sorted(set(v["hit_pieces"]))
        if len(ps) > expand_all:
            keep = {n + d for n in hit for d in (-1, 0, 1)}
            ps = [r for r in ps if r["meta"]["piece"] in keep]
        texts = []
        for a, b in zip([None] + ps, ps):
            if a and b["meta"]["piece"] != a["meta"]["piece"] + 1:
                texts.append("…")
            texts.append(b["text"])
        v.update(text="\n\n".join(texts), complete=len(ps) == v["pieces"], hit_pieces=hit,
                 shown_pieces=[r["meta"]["piece"] for r in ps], related=[])
        out.append(v)
    if with_refs:
        want = {f"{v['path']}::{a}" for v in out for a in filter(None, v["refs"].split(","))} - set(groups)
        rel: dict[str, list[dict]] = {}
        for r in fetch({"group": {"$in": sorted(want)}}) if want else []:
            rel.setdefault(r["meta"]["group"], []).append(r)
        for v in out:
            for a in filter(None, v["refs"].split(",")):
                rs = sorted(rel.get(f"{v['path']}::{a}", []), key=lambda r: r["meta"]["piece"])
                if rs:
                    v["related"].append({"clause_id": a, "title": rs[0]["meta"]["title"],
                                         "text": "\n\n".join(r["text"] for r in rs)})
    return out


def search(query: str, db_dir: Path, strategy: str, embedder: Embedder, k: int = 10,
           service: str | None = None, doc_type: str | None = None, unfavorable: bool = False,
           max_score: float | None = None, with_refs: bool = False) -> list[dict]:
    """조각 단위로 찾고, 같은 조항의 조각을 모아 조항 단위 결과 k 개를 돌려준다."""
    col = _collection(db_dir, collection_name("clauses", strategy, embedder.model), embedder.model)
    conds = []
    if service:
        # 접두어 필터 대신 회사(첫 폴더) 또는 정확한 서비스로 거른다
        conds.append({"service": service} if "/" in service else {"company": service})
    if doc_type:
        conds.append({"doc_type": doc_type})
    if unfavorable:
        conds.append({"is_unfavorable": True})
    if max_score is not None:
        conds.append({"favor_score": {"$lte": max_score}})
    where = conds[0] if len(conds) == 1 else ({"$and": conds} if conds else None)

    def fetch(w):
        got = col.get(where=w, include=["documents", "metadatas"])
        return [{"id": i, "text": d, "meta": m} for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])]

    # 같은 조항의 조각이 여러 개 걸릴 수 있으니, 조항이 k 개 모일 때까지 후보를 늘려 가며 찾는다
    vec, n, total = embedder.query([query]), k * 3, col.count()
    while True:
        r = col.query(query_embeddings=vec, n_results=min(n, total), where=where,
                      include=["documents", "metadatas", "distances"])
        hits = [{"id": i, "text": d, "meta": m, "similarity": round(1 - dist, 3)}
                for i, d, m, dist in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0])]
        groups = {h["meta"]["group"] for h in hits}
        if len(groups) >= k or n >= total or len(hits) < n:
            break
        n *= 3
    # k 번째 조항까지의 조각만 남긴다
    order = list(dict.fromkeys(h["meta"]["group"] for h in hits))[:k]
    hits = [h for h in hits if h["meta"]["group"] in set(order)]
    return expand(hits, fetch, with_refs)
