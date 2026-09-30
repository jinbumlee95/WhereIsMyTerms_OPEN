// 약관 질의응답 화면: POST /api/ask/stream 의 SSE 이벤트(step … result)를 받아 단계·답변·근거를 그린다.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => (typeof p === "number" ? p.toFixed(2) : "-");
const INTENT = { current: "현재 약관", clause_history: "조항 변경 이력", doc_history: "문서 개정 이력" };
const KIND = { C: "현재 조항", T: "조항 변경 이력", H: "변경 기록", D: "문서 개정일", L: "인용 법령" };
const CHANGE = { added: "추가", modified: "수정", removed: "삭제" };   // index.CHANGE_KIND 와 같은 표
const ABSTAIN = {                   // 답변 보류 이유 (flow.abstain_reason)
  not_found: "수록된 약관에서 이 질문과 관련된 내용을 찾지 못했습니다.",
  insufficient: "관련 조항은 찾았지만, 질문 전체에 답할 만큼의 내용을 약관에서 찾지 못했습니다.",
  unknown: "근거를 판정하는 중 오류가 나서 답변을 보류했습니다.",
};

let company = "";            // "" = 전체
let companies = [];
const past = [];             // 이번 세션의 질문과 결과
let thread = [];             // 이어지는 대화 (최근 THREAD_TURNS 턴). 요청마다 보내고, 서버는 저장하지 않는다
const THREAD_TURNS = 3;
const THREAD_ANSWER_CHARS = 600;

// ------------------------------------------------------------------ 회사 선택 (검색·즐겨찾기)
// 즐겨찾기는 이 브라우저의 쿠키(wimt_fav)에만 둔다. 서버는 읽지 않는다.
const FAV_COOKIE = "wimt_fav";
const FAV_DAYS = 365;

function readFavs() {
  const m = document.cookie.match(new RegExp(`(?:^|; )${FAV_COOKIE}=([^;]*)`));
  return m ? decodeURIComponent(m[1]).split(",").filter(Boolean) : [];
}

function writeFavs(ids) {
  document.cookie = `${FAV_COOKIE}=${encodeURIComponent(ids.join(","))}; max-age=${FAV_DAYS * 86400}; path=/; SameSite=Lax`;
}

function toggleFav(id) {
  const favs = readFavs();
  writeFavs(favs.includes(id) ? favs.filter((x) => x !== id) : [...favs, id]);
  renderCompanies();
}

// 회사 이름·폴더 이름·별칭(배그, 마모 …)·문서 제목으로 찾는다 ("마비노기" -> 넥슨). 띄어쓰기·대소문자는 무시
const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, "");
function matches(c, q) {
  return !q || [c.name, c.id, ...(c.aliases || []), ...c.documents.map((d) => d.title)].some((s) => norm(s).includes(q));
}

function companyRow(c, favs) {
  const fav = favs.includes(c.id);
  return `<div class="company-row"><button type="button" class="company" role="radio" data-id="${esc(c.id)}"
      aria-checked="${c.id === company}">${esc(c.name)} <small>문서 ${c.documents.length}</small></button>
    <button type="button" class="fav" data-fav="${esc(c.id)}" aria-pressed="${fav}"
      title="${fav ? "즐겨찾기에서 빼기" : "즐겨찾기에 추가"}" aria-label="${esc(c.name)} ${fav ? "즐겨찾기에서 빼기" : "즐겨찾기에 추가"}">${fav ? "★" : "☆"}</button></div>`;
}

function renderCompanies() {
  const q = norm($("company-search").value);
  const favs = readFavs().filter((id) => companies.some((c) => c.id === id));
  const shown = companies.filter((c) => matches(c, q));
  const starred = favs.map((id) => shown.find((c) => c.id === id)).filter(Boolean);
  const rest = shown.filter((c) => !favs.includes(c.id));
  const all = { id: "", name: "전체", documents: companies.flatMap((c) => c.documents) };
  let html = q ? "" : `<div class="company-row"><button type="button" class="company" role="radio" data-id=""
      aria-checked="${company === ""}">전체 <small>문서 ${all.documents.length}</small></button></div>`;
  if (starred.length) html += `<p class="group">즐겨찾기</p>` + starred.map((c) => companyRow(c, favs)).join("");
  if (rest.length) html += (starred.length ? `<p class="group">회사</p>` : "") + rest.map((c) => companyRow(c, favs)).join("");
  if (!shown.length) html += `<p class="empty">'${esc($("company-search").value)}'에 맞는 회사가 없습니다</p>`;
  const box = $("companies");
  box.innerHTML = html;
  box.querySelectorAll(".company").forEach((b) => b.addEventListener("click", () => selectCompany(b.dataset.id)));
  box.querySelectorAll(".fav").forEach((b) => b.addEventListener("click", () => toggleFav(b.dataset.fav)));
}

async function loadCompanies() {
  companies = await (await fetch("/api/companies")).json();
  $("company-search").addEventListener("input", renderCompanies);
  $("company-search").addEventListener("keydown", (e) => {   // Enter: 첫 번째 결과 선택
    if (e.key !== "Enter") return;
    e.preventDefault();
    const first = $("companies").querySelector(".company");
    if (first) selectCompany(first.dataset.id);
  });
  renderCompanies();
}

function selectCompany(id) {
  company = id;
  document.querySelectorAll(".company").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.id === id)));
  const c = companies.find((x) => x.id === id);
  $("scope").textContent = c ? `${c.name} 약관 ${c.documents.length}개에서 찾습니다` : "전체 회사에서 찾습니다";
}

// ------------------------------------------------------------------ 진행 단계
const PLAN = [
  { node: "contextualize", label: "질문 이해", optional: true },
  { node: "classify", label: "분기 판정" },
  { node: "retrieve", label: "검색" },
  { node: "grade", label: "근거 판정" },
  { node: "laws", label: "법령 조회", optional: true },
  { node: "check_context", label: "문맥 충분성" },
  { node: "expand", label: "추가 검색", optional: true },
  { node: "answer", label: "답변" },
];
let steps = {};
let stepStart = {};          // 진행 중인 단계 -> 시작 시각 (performance.now)
let stepTotal = {};          // 단계 -> 걸린 시간 합계 ms (여러 번 도는 단계는 합친다)

function resetSteps() {
  steps = {};
  stepStart = {};
  stepTotal = {};
  $("steps").innerHTML = PLAN.map((s) => `<li class="step pending" id="step-${s.node}" ${s.optional ? "hidden" : ""}>
      <span class="icon"></span><div><div class="label">${s.label}</div>
      <div class="detail">대기</div><div class="step-time"></div></div></li>`).join("");
}

// 단계별 걸린 시간: 진행 중이면 흐르는 값, 끝나면 합계. 두 번 이상 돈 단계는 횟수도
function showStepTime(node) {
  const el = $(`step-${node}`);
  if (!el) return;
  const ms = (stepTotal[node] || 0) + (stepStart[node] ? performance.now() - stepStart[node] : 0);
  el.querySelector(".step-time").textContent = fmt(ms) + (steps[node] > 1 ? ` · ${steps[node]}회` : "");
}

function tickSteps() {
  Object.keys(stepStart).forEach(showStepTime);
}

function detail(node, info, count) {
  if (!info) return "";
  if (info.error) return node === "classify" ? "판정 실패 → 약관 검색" : "판정 실패";
  switch (node) {
    case "contextualize": return info.rewritten ? `→ ${info.question}` : "새 질문으로 처리";
    case "classify":
      if (info.route === "clarify") return `회사를 여쭤봄 (${pct(info.company_prob)})`;
      return info.route === "rag" ? `약관 검색 필요 (${pct(info.prob)})` : `즉시 답변 (${pct(info.prob)})`;
    case "retrieve": return `근거 ${info.found}건 · ${INTENT[info.intent] || info.intent}` + (info.diverse ? " · 회사별 상위" : "");
    case "grade": return `관련 ${info.relevant}/${info.items}` + (info.cross ? ` · ${info.covered}개 회사` : "")
      + (count > 1 ? ` (${count}번째)` : "");
    case "laws": return `조문 ${info.fetched.length}건` + (info.missing.length ? ` · 없음 ${info.missing.length}` : "")
      + (info.errors.length ? ` · 실패 ${info.errors.length}` : "") + (count > 1 ? ` (${count}번째)` : "");
    case "check_context": return info.status === "unknown" ? "판정 실패" : `충분 ${pct(info.sufficient)}`;
    case "abstain": return { not_found: "약관에서 찾지 못함", unknown: "판정 오류 · 답변 보류" }[info.reason] || "근거 부족 · 답변 보류";
    case "expand": return `${info.round}회차` + (info.widened?.length ? ` · 범위 확대` : ` · 새 검색어`);
    case "answer": return info.insufficient ? `근거 부족 · 인용 ${info.cited}건` : `인용 ${info.cited}건`;
    case "direct_answer": return "문서 없이 답변";
    case "clarify": return "어느 회사인지 확인";
    default: return "";
  }
}

function onStep(ev) {
  let node = ev.node;
  if (node === "direct_answer" || node === "clarify") {  // 즉시 답변·되묻기: 검색·판정은 건너뛴 것으로
    ["retrieve", "grade", "check_context"].forEach((n) => { const el = $(`step-${n}`); el.className = "step skipped"; el.querySelector(".detail").textContent = "건너뜀"; });
    $(`step-answer`).querySelector(".label").textContent = ev.label;
    node = "answer";
  }
  if (node === "abstain") {
    $("step-answer").querySelector(".label").textContent = ev.label;
    node = "answer";
  }
  const el = $(`step-${node}`);
  if (!el) return;
  el.hidden = false;
  if (ev.status === "running") {
    steps[node] = (steps[node] || 0) + 1;
    stepStart[node] = performance.now();
    showStepTime(node);
    el.className = "step running";
    el.querySelector(".detail").textContent = node === "grade" && steps[node] > 1 ? "다시 판정 중…" : "진행 중…";
    if (node === "grade" || node === "expand") {        // 다시 판정·추가 검색 중에는 뒤 단계를 대기로
      $("step-answer").className = "step pending";
    }
  } else {
    if (stepStart[node]) {
      stepTotal[node] = (stepTotal[node] || 0) + performance.now() - stepStart[node];
      delete stepStart[node];
    }
    showStepTime(node);
    el.className = `step ${ev.status}`;
    el.querySelector(".icon").textContent = ev.status === "done" ? "✓" : "!";
    el.querySelector(".detail").textContent = detail(ev.node, ev.info, steps[node]);
  }
}

// ------------------------------------------------------------------ 답변·근거
function renderAnswer(text) {
  const withCites = esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([CTHDL]\d+)\]/g, (_, t) => `<button type="button" class="cite" data-tag="${t}">${t}</button>`);
  return withCites.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
}

// 근거 본문에서 답변이 참고한 부분(서버가 준 [시작, 끝] 구간)을 <mark> 로 감싼다. 나머지는 그대로 이스케이프
function marked(text, ranges) {
  let out = "", at = 0;
  for (const [s, e] of ranges) {
    if (s < at || e > text.length) continue;
    out += esc(text.slice(at, s)) + `<mark class="hl">${esc(text.slice(s, e))}</mark>`;
    at = e;
  }
  return out + esc(text.slice(at));
}

function renderResult(r, question) {
  $("result").hidden = false;
  $("understood").hidden = !r.rewritten;               // 이어진 질문을 어떻게 이해했는지 (틀렸으면 바로 알 수 있게)
  $("understood").textContent = r.rewritten ? `이렇게 이해했어요: ${r.question}` : "";
  $("answer").innerHTML = renderAnswer(r.answer);
  const cited = new Set(r.citations.map((c) => c.tag));
  $("notice").hidden = !r.insufficient;
  $("notice").textContent = ABSTAIN[r.abstain_reason] || ABSTAIN.insufficient;
  $("choices").hidden = r.route !== "clarify";
  if (r.route === "clarify") {                          // 되묻기: 회사를 고르면 같은 질문을 다시 보낸다
    $("choices").innerHTML = r.choices.map((c) => `<button type="button" data-id="${esc(c.id)}">${esc(c.name)}</button>`).join("")
      + `<button type="button" class="all" data-id="">전체 회사에서 찾기</button>`;
    $("choices").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      selectCompany(b.dataset.id);
      ask(question, { allCompanies: !b.dataset.id });
    }));
  }
  const more = r.suggestions || [];
  $("suggest").hidden = !more.length;             // 추천 질문: 누르면 이전 대화 없이 새 질문으로 보낸다
  $("suggest-list").innerHTML = more.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("suggest-list").querySelectorAll("button").forEach((b, i) => b.addEventListener("click", () => ask(more[i])));
  const meta = [];
  if (r.route === "direct") meta.push(`즉시 답변 (분기 ${pct(r.route_prob)})`);
  else if (r.route === "clarify") meta.push(`회사 불명 (${pct(r.company_prob)}) · 검색하지 않음`);
  else {
    meta.push(`분기 ${pct(r.route_prob)}`, r.sufficiency_status === "unknown" ? "충분성 판정 실패" : `충분 ${pct(r.sufficient_prob)}`);
    if (r.expansions) meta.push(`추가 검색 ${r.expansions}회`);
    meta.push(`근거 ${r.evidence.length}건 중 인용 ${cited.size}건`);
  }
  if (r.elapsed_ms) meta.push(`처리 ${(r.elapsed_ms / 1000).toFixed(1)}초`);
  $("meta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");

  // 근거: 약관(C·T·H·D)과 법령(L)을 나눠 보여 준다. 법령 근거가 없으면 "근거" 한 구역만
  const card = (e) => {
    // 조항 변경 이력처럼 날짜가 여러 개면 가장 최근 날짜만 "… 등 n건"으로 (전체는 마우스를 올리면 보인다)
    const dates = String(e.version_date || "").split(",").map((d) => d.trim()).filter(Boolean);
    const when = dates.length > 1 ? `${dates[dates.length - 1]} 등 ${dates.length}건` : dates[0] || "";
    const sub = [KIND[e.tag[0]], when, CHANGE[e.change_type] || e.change_type].filter(Boolean).join(" · ");
    const law = e.tag[0] === "L";
    const hl = e.highlights || [];
    return `<details class="ev ${law ? "law" : ""} ${cited.has(e.tag) ? "cited" : ""}" id="ev-${e.tag}">
      <summary><span class="tag">${esc(e.tag)}</span><span class="head">${esc(e.head.replace(/^\[[CTHDL]\d+\]\s*/, ""))}</span>
      ${hl.length ? `<span class="hl-count" title="답변 문장과 가장 비슷한 근거 문장 (글자 비교로 찾은 것)">참고 ${hl.length}곳</span>` : ""}
      <span class="sub" ${dates.length > 1 ? `title="${esc(dates.join(", "))}"` : ""}>${esc(sub)}</span>${e.source_url ? `<a class="source" href="${esc(e.source_url)}" target="_blank"
      rel="noopener" title="${law ? "국가법령정보센터 (법령 원문)" : "해당 기업의 공식 페이지 (정본)"}">원문 확인 ↗</a>` : ""}</summary><pre>${marked(e.text, hl)}</pre></details>`;
  };
  const terms = r.evidence.filter((e) => e.tag[0] !== "L");
  const laws = r.evidence.filter((e) => e.tag[0] === "L");
  $("evidence-wrap").hidden = !r.evidence.length;
  $("ev-terms-wrap").hidden = !terms.length;
  $("ev-terms-title").textContent = laws.length ? "약관 근거" : "근거";
  $("ev-terms-count").textContent = terms.length ? `${terms.length}건` : "";
  $("ev-terms").innerHTML = terms.map(card).join("");
  $("ev-laws-wrap").hidden = !laws.length;
  $("ev-laws-count").textContent = laws.length ? `${laws.length}건` : "";
  $("ev-laws").innerHTML = laws.map(card).join("");
  $("trace").textContent = JSON.stringify(r.trace, null, 2);
  $("answer").querySelectorAll(".cite").forEach((b) => b.addEventListener("click", () => {
    const ev = $(`ev-${b.dataset.tag}`);
    if (!ev) return;
    ev.open = true;
    (ev.querySelector("mark.hl") || ev).scrollIntoView({ behavior: "smooth", block: "center" });   // 참고한 부분으로
    ev.classList.add("flash");
    setTimeout(() => ev.classList.remove("flash"), 1200);
  }));
}

function addHistory(question, companyId, result) {
  past.unshift({ question, companyId, result });
  $("history").innerHTML = past.map((h, i) => `<li><button type="button" data-i="${i}" title="${esc(h.question)}">${esc(h.question)}</button></li>`).join("");
  $("history").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const h = past[Number(b.dataset.i)];
    $("question").value = h.question;
    selectCompany(h.companyId);
    $("progress").hidden = true;
    $("error").hidden = true;
    renderResult(h.result, h.question);
  }));
}

// ------------------------------------------------------------------ 진행 시간 타이머
let timer = null;
const fmt = (ms) => `${(ms / 1000).toFixed(1)}초`;

function startTimer() {
  const t0 = performance.now();
  $("progress-title").textContent = "처리 중";
  $("progress").classList.remove("done", "failed");
  $("timer").textContent = fmt(0);
  clearInterval(timer);
  timer = setInterval(() => { $("timer").textContent = fmt(performance.now() - t0); tickSteps(); }, 100);
  return () => performance.now() - t0;
}

function stopTimer(ms, ok) {
  clearInterval(timer);
  Object.keys(stepStart).forEach((n) => {           // 중단됐으면 진행 중이던 단계도 그 시점에서 멈춘다
    stepTotal[n] = (stepTotal[n] || 0) + performance.now() - stepStart[n];
    delete stepStart[n];
    showStepTime(n);
  });
  $("progress-title").textContent = ok ? "완료" : "중단";
  $("progress").classList.add(ok ? "done" : "failed");
  $("timer").textContent = fmt(ms);
}

// ------------------------------------------------------------------ 질문 보내기 (SSE)
async function ask(question, { allCompanies = false } = {}) {
  $("question").value = question;
  $("submit").disabled = true;
  $("error").hidden = true;
  $("result").hidden = true;
  $("evidence-wrap").hidden = true;
  $("progress").hidden = false;
  $("status").hidden = true;
  resetSteps();
  const elapsed = startTimer();
  let ok = false;
  try {
    const res = await fetch("/api/ask/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, company: company || null, all_companies: allCompanies, history: thread }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `요청 실패 (${res.status})`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const data = chunk.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
        if (!data) continue;
        const ev = JSON.parse(data);
        if (ev.type === "status") { $("status").hidden = false; $("status").textContent = ev.message; }
        else if (ev.type === "step") { $("status").hidden = true; onStep(ev); }
        else if (ev.type === "result") {
          ev.elapsed_ms = elapsed();                    // 걸린 시간 (이번 세션 목록에서 다시 볼 때도 보이게 결과에 담는다)
          stopTimer(ev.elapsed_ms, true);
          ok = true;
          renderResult(ev, question); addHistory(question, company, ev); addTurn(ev);
        }
        else if (ev.type === "error") throw new Error(ev.message);
      }
    }
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = e.message;
    document.querySelectorAll(".step.running").forEach((el) => { el.className = "step error"; el.querySelector(".icon").textContent = "!"; });
  } finally {
    if (!ok) stopTimer(elapsed(), false);
    $("submit").disabled = false;
  }
}

// ------------------------------------------------------------------ 이어지는 대화 (멀티턴)
// 서버는 대화를 기억하지 않는다. 화면이 최근 턴을 요청에 담아 보내고, 서버는 새 질문을 독립 질문으로 바꾸는 데만 쓴다.
function addTurn(r) {
  if (r.route === "clarify") return;                   // 되묻기는 대화에 넣지 않는다 (회사를 고르면 같은 질문을 다시 보낸다)
  const answer = String(r.answer || "").replace(/\[[CTHDL]\d+\]/g, "").slice(0, THREAD_ANSWER_CHARS);
  thread = [...thread, { question: r.question, answer, company: company || null }].slice(-THREAD_TURNS);
  renderThread();
}

function renderThread() {
  $("thread").hidden = $("new-thread").hidden = !thread.length;
  $("thread").textContent = thread.length ? `대화 이어가는 중 (${thread.length}턴)` : "";
}

$("new-thread").addEventListener("click", () => {
  thread = [];
  renderThread();
  $("question").focus();
});

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) ask(q);
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("ask").requestSubmit(); }
});
document.querySelectorAll(".examples button").forEach((b) => b.addEventListener("click", () => {
  $("question").value = b.dataset.q;
  selectCompany(b.dataset.c);
  $("question").focus();
}));
loadCompanies().then(() => selectCompany(""));
