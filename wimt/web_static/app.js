// 약관 질의응답 화면: POST /api/ask/stream 의 SSE 이벤트(step … result)를 받아 단계·답변·근거를 그린다.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => (typeof p === "number" ? p.toFixed(2) : "-");
const INTENT = { current: "현재 약관", clause_history: "조항 변경 이력", doc_history: "문서 개정 이력" };
const KIND = { C: "현재 조항", T: "조항 변경 이력", H: "변경 기록", D: "문서 개정일", L: "인용 법령" };
const ABSTAIN = {                   // 답변 보류 이유 (flow.abstain_reason)
  not_found: "수록된 약관에서 이 질문과 관련된 내용을 찾지 못했습니다.",
  insufficient: "관련 조항은 찾았지만, 질문 전체에 답할 만큼의 내용을 약관에서 찾지 못했습니다.",
  unknown: "근거를 판정하는 중 오류가 나서 답변을 보류했습니다.",
};

let company = "";            // "" = 전체
let companies = [];
const past = [];             // 이번 세션의 질문과 결과

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
  { node: "classify", label: "분기 판정" },
  { node: "retrieve", label: "검색" },
  { node: "grade", label: "근거 판정" },
  { node: "laws", label: "법령 조회", optional: true },
  { node: "check_context", label: "문맥 충분성" },
  { node: "expand", label: "추가 검색", optional: true },
  { node: "answer", label: "답변" },
];
let steps = {};

function resetSteps() {
  steps = {};
  $("steps").innerHTML = PLAN.map((s) => `<li class="step pending" id="step-${s.node}" ${s.optional ? "hidden" : ""}>
      <span class="icon"></span><div><div class="label">${s.label}</div><div class="detail">대기</div></div></li>`).join("");
}

function detail(node, info, count) {
  if (!info) return "";
  if (info.error) return node === "classify" ? "판정 실패 → 약관 검색" : "판정 실패";
  switch (node) {
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
    el.className = "step running";
    el.querySelector(".detail").textContent = node === "grade" && steps[node] > 1 ? "다시 판정 중…" : "진행 중…";
    if (node === "grade" || node === "expand") {        // 다시 판정·추가 검색 중에는 뒤 단계를 대기로
      $("step-answer").className = "step pending";
    }
  } else {
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

function renderResult(r, question) {
  $("result").hidden = false;
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
  $("meta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");

  $("evidence-wrap").hidden = !r.evidence.length;
  $("evidence-count").textContent = r.evidence.length ? `${r.evidence.length}건` : "";
  $("evidence").innerHTML = r.evidence.map((e) => {
    const sub = [KIND[e.tag[0]], e.version_date, e.change_type].filter(Boolean).join(" · ");
    return `<details class="ev ${cited.has(e.tag) ? "cited" : ""}" id="ev-${e.tag}">
      <summary><span class="tag">${esc(e.tag)}</span><span class="head">${esc(e.head.replace(/^\[[CTHDL]\d+\]\s*/, ""))}</span>
      <span class="sub">${esc(sub)}</span>${e.source_url ? `<a class="source" href="${esc(e.source_url)}" target="_blank"
      rel="noopener" title="해당 기업의 공식 페이지 (정본)">원문 확인 ↗</a>` : ""}</summary><pre>${esc(e.text)}</pre></details>`;
  }).join("");
  $("trace").textContent = JSON.stringify(r.trace, null, 2);
  $("answer").querySelectorAll(".cite").forEach((b) => b.addEventListener("click", () => {
    const ev = $(`ev-${b.dataset.tag}`);
    if (!ev) return;
    ev.open = true;
    ev.scrollIntoView({ behavior: "smooth", block: "center" });
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
  try {
    const res = await fetch("/api/ask/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, company: company || null, all_companies: allCompanies }),
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
        else if (ev.type === "result") { renderResult(ev, question); addHistory(question, company, ev); }
        else if (ev.type === "error") throw new Error(ev.message);
      }
    }
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = e.message;
    document.querySelectorAll(".step.running").forEach((el) => { el.className = "step error"; el.querySelector(".icon").textContent = "!"; });
  } finally {
    $("submit").disabled = false;
  }
}

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
