"""피드백 웹 UI: 검사표·라벨링 시트를 브라우저에서 버튼으로 채운다 (python -m wimt ui).

CSV 파일(reports/*.csv)이 그대로 저장소다. 버튼을 누를 때마다 해당 행을 채워 CSV 를 다시 쓴다.
그래서 엑셀로 열어 보거나 label-compare 로 넘기는 흐름은 그대로다. 표준 라이브러리만 쓴다.
"""
import difflib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from . import reports

# 시트 이름 -> (파일, 답 열, 선택지, 설명[, 고쳐 쓸 수 있는 열])
SHEETS = {
    "change": ("change_review.csv", "가짜변경(Y/N)",
               [("N", "실제 변경"), ("Y", "가짜 변경")],
               "감지된 변경 무작위 표본. 내용이 실제로 바뀌었는지, 띄어쓰기·서식만 바뀐 가짜 변경인지 가립니다."),
    "drop": ("drop_review.csv", "판정(실제 불리/채점 흔들림/기타)",
             [("실제 불리", "실제 불리"), ("채점 흔들림", "채점 흔들림"), ("기타", "기타")],
             "항 지수가 크게 떨어져 '불리해진 조항'이 된 수정 전부. 실제로 불리해졌는지, 채점이 흔들린 것인지 가립니다."),
    "label": ("labeling_sheet.csv", "사람라벨(-2~2)",
              [("-2", "매우 불리"), ("-1", "불리"), ("0", "중립"), ("1", "유리"), ("2", "매우 유리")],
              "항 단위 유불리 라벨. 모델 지수는 일부러 보여 주지 않습니다. 다 채우면 아래에서 지수와 비교합니다."),
    "qa": ("qa_sheet.csv", "판정(채택/버림)",
           [("채택", "채택"), ("버림", "버림")],
           "RAG 평가 질문 초안. 아래 조항(정답)만 보고 답할 수 있는, 사용자가 실제로 할 법한 질문이면 채택합니다. "
           "질문을 고친 뒤 채택해도 됩니다.", "question"),
}
TAB_NAMES = {"change": "변경 검사", "drop": "지수 하락 검사", "label": "라벨링", "qa": "평가 질문"}
MEMO = "메모"
_TOKEN = re.compile(r"\s+|[^\s]+")


def word_diff(old: str, new: str) -> list[list[str]]:
    """[["=", 텍스트], ["-", 지운 것], ["+", 넣은 것], ...] (공백을 보존한 단어 단위)."""
    a, b = _TOKEN.findall(old or ""), _TOKEN.findall(new or "")
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(["=", "".join(a[i1:i2])])
            continue
        if i2 > i1:
            out.append(["-", "".join(a[i1:i2])])
        if j2 > j1:
            out.append(["+", "".join(b[j1:j2])])
    return out


class App:
    def __init__(self, reports_dir: Path, builders: dict, compare):
        self.dir, self.builders, self.compare = reports_dir, builders, compare

    def path(self, name: str) -> Path:
        return self.dir / SHEETS[name][0]

    def rows(self, name: str) -> list[dict]:
        p = self.path(name)
        return reports.read_csv(p) if p.exists() else []

    def summary(self) -> list[dict]:
        out = []
        for name, (file, col, options, desc, *_) in SHEETS.items():
            rows = self.rows(name)
            out.append({"name": name, "tab": TAB_NAMES[name], "file": file, "desc": desc, "options": options, "total": len(rows),
                        "done": sum(1 for r in rows if (r.get(col) or "").strip()), "exists": self.path(name).exists()})
        return out

    def sheet(self, name: str) -> dict:
        _, col, options, desc, *edit = SHEETS[name]
        items = []
        for r in self.rows(name):
            old, new = r.get("old_text") or "", r.get("new_text") or ""
            items.append({"row": r, "answer": r.get(col, ""), "memo": r.get(MEMO, ""),
                          "diff": word_diff(old, new) if (old and new) else None})
        return {"name": name, "col": col, "options": options, "desc": desc, "edit": edit[0] if edit else None,
                "items": items}

    def answer(self, name: str, i: int, answer: str, memo: str, edit: str | None = None) -> dict:
        _, col, options, _, *edit_col = SHEETS[name]
        if answer and answer not in {v for v, _ in options}:
            raise ValueError(f"선택지에 없는 값: {answer}")
        rows = self.rows(name)
        rows[i][col], rows[i][MEMO] = answer, memo
        if edit_col and edit is not None and edit.strip():
            rows[i][edit_col[0]] = edit.strip()
        reports.write_csv(self.path(name), rows)
        return {"ok": True}

    def regenerate(self, name: str, force: bool) -> dict:
        _, col, *_ = SHEETS[name]
        if name not in self.builders:
            return {"ok": False, "error": "이 시트는 명령줄에서 만듭니다 (python -m wimt qa-draft)"}
        done = sum(1 for r in self.rows(name) if (r.get(col) or "").strip())
        if done and not force:
            return {"ok": False, "done": done}
        rows = self.builders[name]()
        reports.write_csv(self.path(name), rows)
        return {"ok": True, "total": len(rows)}


def serve(app: App, host: str = "127.0.0.1", port: int = 8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method: str):
            parts = [unquote(p) for p in self.path.split("?")[0].strip("/").split("/")]
            try:
                if method == "GET" and parts == [""]:
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if method == "GET" and parts == ["api", "sheets"]:
                    return self._send(200, app.summary())
                if parts[:2] == ["api", "sheet"] and parts[2] in SHEETS:
                    if method == "GET" and len(parts) == 3:
                        return self._send(200, app.sheet(parts[2]))
                    if method == "POST" and parts[3:] == ["regenerate"]:
                        return self._send(200, app.regenerate(parts[2], bool(self._body().get("force"))))
                    if method == "POST" and len(parts) == 4 and parts[3].isdigit():
                        b = self._body()
                        return self._send(200, app.answer(parts[2], int(parts[3]), b.get("answer", ""), b.get("memo", ""),
                                                          b.get("edit")))
                if method == "POST" and parts == ["api", "label-compare"]:
                    _rows, summary = app.compare(self._body().get("scorer", "baseline"))
                    return self._send(200, summary)
                self._send(404, {"error": "not found"})
            except (ValueError, IndexError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:           # 로컬 도구라 원인을 그대로 보여 준다
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"피드백 UI: http://{host}:{port}  (끝내려면 Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WIMT 피드백</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e3e2dd;--acc:#2b5fd9;--del:#fde2e1;--delk:#a4271c;--ins:#dcf3e1;--insk:#1c6b34;--sel:#eef2fc}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe6;--mute:#9b9a94;--line:#34332f;--acc:#7ea2ff;--del:#4a2220;--delk:#ffb3aa;--ins:#1d3a26;--insk:#a6e6b8;--sel:#262c3d}}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 system-ui,-apple-system,"Malgun Gothic",sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;gap:8px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2;flex-wrap:wrap}
header h1{font-size:15px;margin:0 12px 0 0}
.tab{border:1px solid var(--line);background:none;color:var(--ink);padding:6px 12px;border-radius:6px;cursor:pointer}
.tab.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.tab small{color:var(--mute);margin-left:6px;font-weight:400}
main{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 52px)}
@media (max-width:760px){main{grid-template-columns:1fr}#list{max-height:30vh}}
#list{border-right:1px solid var(--line);overflow:auto;max-height:calc(100vh - 52px);position:sticky;top:52px}
#list div{padding:7px 12px;border-bottom:1px solid var(--line);cursor:pointer;font-size:12.5px;display:flex;gap:8px;align-items:baseline}
#list div.on{background:var(--sel)}#list .dot{width:8px;height:8px;border-radius:50%;background:var(--line);flex:none}
#list .dot.done{background:var(--acc)}#list .ans{margin-left:auto;color:var(--mute)}
#view{padding:20px 24px;max-width:1000px}
.desc{color:var(--mute);margin:0 0 16px}.meta{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--mute);font-size:13px;margin-bottom:12px}
.meta b{color:var(--ink);font-weight:600}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;white-space:pre-wrap;word-break:break-word;max-height:55vh;overflow:auto}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media (max-width:760px){.cols{grid-template-columns:1fr}}
.lbl{font-size:12px;color:var(--mute);margin:12px 0 4px}
del{background:var(--del);color:var(--delk);text-decoration:line-through}ins{background:var(--ins);color:var(--insk);text-decoration:none}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 10px}
.btns button{padding:10px 16px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer;font-size:14px}
.btns button.on{background:var(--acc);border-color:var(--acc);color:#fff}.btns kbd{opacity:.6;margin-right:6px;font-size:12px}
input.edit{width:100%;font:inherit;font-size:16px;font-weight:600;padding:10px;border:1px solid var(--acc);border-radius:8px;background:var(--panel);color:var(--ink);margin-bottom:6px}
textarea{width:100%;min-height:54px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);padding:8px;font:inherit}
.nav{display:flex;gap:8px;align-items:center;margin-top:10px;color:var(--mute);font-size:13px}
.nav button,.tool button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:5px 10px;cursor:pointer}
.tool{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);display:flex;gap:8px;align-items:center;flex-wrap:wrap;color:var(--mute);font-size:13px}
pre.out{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;white-space:pre-wrap}
.empty{color:var(--mute);padding:40px 0}
</style></head><body>
<header><h1>WIMT 피드백</h1><span id="tabs"></span></header>
<main><nav id="list"></nav><section id="view"></section></main>
<script>
const $=s=>document.querySelector(s);
let sheets=[],cur=null,data=null,idx=0,armed=false;
const esc=s=>(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function api(p,body){const r=await fetch(p,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error);return j}
async function loadTabs(){sheets=await api("/api/sheets");$("#tabs").innerHTML=sheets.map(s=>`<button class="tab ${s.name===cur?"on":""}" data-n="${s.name}">${s.tab}<small>${s.done}/${s.total}</small></button>`).join("");
  document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>open(b.dataset.n))}
async function open(name,keep){cur=name;armed=false;data=await api("/api/sheet/"+name);await loadTabs();
  if(!keep){const f=data.items.findIndex(it=>!it.answer);idx=f<0?0:f}render()}
function label(v){const o=data.options.find(o=>o[0]===v);return o?o[1]:""}
function render(){
  $("#list").innerHTML=data.items.map((it,i)=>{const r=it.row;return `<div class="${i===idx?"on":""}" data-i="${i}"><span class="dot ${it.answer?"done":""}"></span><span>${esc(r.clause_id)} · ${esc((r.doc||"").split("/").pop().replace(".md",""))}</span><span class="ans">${esc(label(it.answer))}</span></div>`}).join("");
  document.querySelectorAll("#list div").forEach(d=>d.onclick=()=>{idx=+d.dataset.i;render()});
  const on=$("#list .on");on&&on.scrollIntoView({block:"nearest"});
  const v=$("#view");
  if(!data.items.length){v.innerHTML=`<p class="desc">${esc(data.desc)}</p><div class="empty">시트가 비어 있거나 없습니다.</div>${tools()}`;bindTools();return}
  const it=data.items[idx],r=it.row;
  const meta=[["종류",{current:"현재 조항",history:"변경 이력"}[r.kind]],["문서",r.doc],["조항",r.clause_id],["버전",r.version_date],["변경",r.change_type],["문서 종류",r.doc_type],["조 제목",r.title],
    ["지수",r.old_score?`${r.old_score} → ${r.favor_score} (${r.score_delta})`:(data.name==="label"?"":r.favor_score)],["유사도",r.similarity],["커밋",r.commit]]
    .filter(m=>m[1]).map(m=>`<span>${m[0]} <b>${esc(String(m[1]))}</b></span>`).join("");
  let body;
  if(it.diff){body=`<div class="lbl">변경 (지운 곳 / 넣은 곳)</div><div class="box">${it.diff.map(([op,t])=>op==="="?esc(t):op==="-"?`<del>${esc(t)}</del>`:`<ins>${esc(t)}</ins>`).join("")}</div>
    <details><summary class="lbl">전·후 원문 나란히</summary><div class="cols"><div class="box">${esc(r.old_text)}</div><div class="box">${esc(r.new_text)}</div></div></details>`}
  else{const t=r.text||r.new_text||r.old_text;body=`<div class="lbl">${r.change_type==="removed"?"삭제된 조항":r.change_type==="added"?"추가된 조항":"조항"}</div><div class="box">${esc(t)}</div>`}
  const edit=data.edit?`<div class="lbl">질문 (고쳐 쓴 뒤 채택 가능)</div><input id="edit" class="edit" value="${esc(r[data.edit]).replace(/"/g,"&quot;")}">`:"";
  v.innerHTML=`<p class="desc">${esc(data.desc)}</p><div class="meta">${meta}</div>${edit}${body}
  <div class="btns">${data.options.map(([val,lab],k)=>`<button data-v="${esc(val)}" class="${it.answer===val?"on":""}"><kbd>${k+1}</kbd>${esc(lab)}</button>`).join("")}</div>
  <textarea id="memo" placeholder="메모 (선택)">${esc(it.memo)}</textarea>
  <div class="nav"><button id="prev">← 이전</button><button id="next">다음 →</button><span>${idx+1} / ${data.items.length} · 숫자 키로 선택, ←/→ 이동</span></div>${tools()}`;
  document.querySelectorAll(".btns button").forEach(b=>b.onclick=()=>save(b.dataset.v));
  $("#prev").onclick=()=>move(-1);$("#next").onclick=()=>move(1);
  $("#memo").onchange=()=>save(it.answer,true);if($("#edit"))$("#edit").onchange=()=>save(it.answer,true);bindTools()}
function tools(){return `<div class="tool">${data.name==="label"?`<button data-s="baseline">지수와 비교 (baseline)</button><button data-s="jev">지수와 비교 (jev)</button>`:""}
  <button id="regen">${armed?"정말 새로 만들기 (입력한 답이 지워짐)":"시트 새로 만들기"}</button><span id="msg"></span></div><pre class="out" id="out" hidden></pre>`}
function bindTools(){document.querySelectorAll(".tool [data-s]").forEach(b=>b.onclick=async()=>{$("#msg").textContent="비교 중…";
    try{const s=await api("/api/label-compare",{scorer:b.dataset.s});$("#out").hidden=false;$("#out").textContent=JSON.stringify(s,null,2);$("#msg").textContent="labeling_compare.csv 에 저장"}catch(e){$("#msg").textContent=e.message}});
  $("#regen").onclick=async()=>{const r=await api(`/api/sheet/${cur}/regenerate`,{force:armed});
    if(r.error){$("#msg").textContent=r.error;return}
    if(!r.ok){armed=true;render();$("#msg").textContent=`이미 ${r.done}건 입력됨. 한 번 더 누르면 지우고 새로 만듭니다.`;return}await open(cur)}}
function move(d){idx=Math.max(0,Math.min(data.items.length-1,idx+d));render()}
async function save(val,stay){const it=data.items[idx];const memo=$("#memo").value;const ed=$("#edit");
  if(!stay&&it.answer===val)val="";           // 같은 버튼을 다시 누르면 취소
  await api(`/api/sheet/${cur}/${idx}`,{answer:val,memo,edit:ed?ed.value:null});it.answer=val;it.memo=memo;if(ed&&ed.value.trim())it.row[data.edit]=ed.value.trim();
  if(!stay&&val){const n=data.items.findIndex((x,i)=>i>idx&&!x.answer);if(n>=0)idx=n}
  await loadTabs();render()}
document.addEventListener("keydown",e=>{if(!data||["TEXTAREA","INPUT"].includes(e.target.tagName))return;
  const k=parseInt(e.key);if(k>=1&&k<=data.options.length){save(data.options[k-1][0]);return}
  if(e.key==="ArrowRight")move(1);if(e.key==="ArrowLeft")move(-1)});
loadTabs().then(()=>open("change"));
</script></body></html>
"""
