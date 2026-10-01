// 약관 질의응답 화면: POST /api/ask/stream 의 SSE 이벤트(step … result)를 받아 단계·답변·근거를 그린다.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => (typeof p === "number" ? p.toFixed(2) : "-");
const INTENT = { current: "수집본 약관", clause_history: "조항 변경 이력", doc_history: "문서 개정 이력" };
const KIND = { C: "수집본 조항", T: "조항 변경 이력", H: "변경 기록", D: "문서 개정일", L: "인용 법령" };
// 근거 번호(C1, T1 …)에 마우스를 올리면 보이는 설명
// 약관은 각 회사의 저작물이고 이 서비스의 사본은 수집본이므로, 정본은 회사 공식 페이지라고 함께 밝힌다
const KIND_HELP = {
  C: "수집본 조항: 이 서비스가 마지막으로 수집한 약관의 조항",
  T: "조항 변경 이력: 수집본 기준으로 한 조항이 개정 때마다 어떻게 바뀌었는지 날짜순으로",
  H: "변경 기록: 수집본 사이에서 찾은 개별 변경 (추가·삭제·수정)",
  D: "문서 개정일: 이 서비스가 수집한 문서 개정 날짜 목록",
  L: "인용 법령: 약관이 따른다고 한 법령의 조문",
};
const TIP_SOURCE = { L: "법령 원문은 국가법령정보센터에서 확인하세요." };
const TERMS_SOURCE = "최신이 아닐 수 있으며, 정본은 해당 기업의 공식 페이지입니다.";

// 바로 뜨는 말풍선: data-tip 을 가진 요소에 마우스를 올리거나 키보드로 초점을 주면 위에 띄운다
const tipBox = document.createElement("div");
tipBox.className = "tip";
tipBox.setAttribute("role", "tooltip");
tipBox.hidden = true;
document.body.appendChild(tipBox);
function showTip(el) {
  tipBox.textContent = el.dataset.tip;
  tipBox.hidden = false;
  const r = el.getBoundingClientRect(), w = tipBox.offsetWidth, h = tipBox.offsetHeight;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8));
  const top = r.top - h - 8 >= 8 ? r.top - h - 8 : r.bottom + 8;     // 위에 자리가 없으면 아래로
  tipBox.style.left = `${left + scrollX}px`;
  tipBox.style.top = `${top + scrollY}px`;
}
const hideTip = () => { tipBox.hidden = true; };
for (const [on, off] of [["mouseover", "mouseout"], ["focusin", "focusout"]]) {
  document.addEventListener(on, (e) => { const el = e.target.closest?.("[data-tip]"); if (el) showTip(el); });
  document.addEventListener(off, (e) => { if (e.target.closest?.("[data-tip]")) hideTip(); });
}
addEventListener("scroll", hideTip, true);
const CHANGE = { added: "추가", modified: "수정", removed: "삭제" };   // index.CHANGE_KIND 와 같은 표
const ABSTAIN = {                   // 답변 보류 이유 (flow.abstain_reason)
  not_found: "수록된 약관에서 이 질문과 관련된 내용을 찾지 못했습니다.",
  insufficient: "관련 조항은 찾았지만, 질문 전체에 답할 만큼의 내용을 약관에서 찾지 못했습니다.",
  unknown: "근거를 판정하는 중 오류가 나서 답변을 보류했습니다.",
};

let company = "";            // "" = 전체
let companies = [];
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

function selectCompany(id, { keepConv = false } = {}) {
  company = id;
  if (!keepConv && active && active.company !== id) closeConv();   // 다른 회사로 옮기면 그 회사의 새 대화로
  renderConvs();
  document.querySelectorAll(".company").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.id === id)));
  const box = $("companies"), row = box.querySelector(".company[aria-checked='true']");   // 스크롤 밖이면 목록 안에서만 보이게
  if (row && (row.offsetTop < box.scrollTop || row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight)) {
    box.scrollTop = row.offsetTop - box.clientHeight / 2;
  }
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
      if (active) active.company = b.dataset.id;          // 되묻기 답은 지금 대화를 그 회사의 대화로 이어 간다
      selectCompany(b.dataset.id, { keepConv: true });
      ask(question, { allCompanies: !b.dataset.id });
    }));
  }
  const more = r.suggestions || [];
  // 추천 질문: 질문이 가리키는 회사가 하나면 그 회사로 바꿔 묻는다. 다른 회사로 바뀌면 그 회사의 새 대화가 된다
  const where = r.suggestion_companies || [];
  $("suggest").hidden = !more.length;
  $("suggest-list").innerHTML = more.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("suggest-list").querySelectorAll("button").forEach((b, i) => b.addEventListener("click", () => {
    if (where[i] && where[i] !== company && companies.some((c) => c.id === where[i])) selectCompany(where[i]);
    ask(more[i]);
  }));
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
  // 근거 번호 설명: 종류 + 어느 문서의 어느 조항인지
  const heads = Object.fromEntries(r.evidence.map((e) => [e.tag, e.head.replace(/^\[[CTHDL]\d+\]\s*/, "")]));
  const source = (k) => TIP_SOURCE[k] || TERMS_SOURCE;
  const tip = (tag) => [KIND_HELP[tag[0]], heads[tag], source(tag[0])].filter(Boolean).join("\n");
  $("answer").querySelectorAll(".cite").forEach((b) => { b.dataset.tip = tip(b.dataset.tag) + "\n(누르면 해당 근거로 이동)"; });
  document.querySelectorAll("#evidence-wrap .ev .tag").forEach((t) => {
    const k = t.textContent[0];
    if (KIND_HELP[k]) t.dataset.tip = KIND_HELP[k] + "\n" + source(k);
  });
  $("trace").textContent = r.trace ? JSON.stringify(r.trace, null, 2) : "(저장된 대화에는 처리 기록을 남기지 않습니다)";
  $("answer").querySelectorAll(".cite").forEach((b) => b.addEventListener("click", () => {
    const ev = $(`ev-${b.dataset.tag}`);
    if (!ev) return;
    ev.open = true;
    (ev.querySelector("mark.hl") || ev).scrollIntoView({ behavior: "smooth", block: "center" });   // 참고한 부분으로
    ev.classList.add("flash");
    setTimeout(() => ev.classList.remove("flash"), 1200);
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
          ev.elapsed_ms = elapsed();                    // 걸린 시간 (최근 대화에서 다시 볼 때도 보이게 결과에 담는다)
          stopTimer(ev.elapsed_ms, true);
          ok = true;
          renderResult(ev, question); addTurn(ev);
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
// 대화는 회사가 달린 채로 이 브라우저의 localStorage(wimt_convs)에만 저장해, 오른쪽 목록에서 골라 나중에 이어 간다.
const CONV_KEY = "wimt_convs";
const CONV_MAX = 5;                // 회사와 상관없이 최근에 이어 간 대화 n개만 남긴다
let convs = loadConvs();           // [{id, company, created, updated, turns: [{question, answer, result}]}]
let active = null;                 // 지금 이어 가는 대화 (없으면 다음 질문이 새 대화를 연다)

function loadConvs() {
  try {
    const v = JSON.parse(localStorage.getItem(CONV_KEY) || "[]");
    return Array.isArray(v) ? v.filter((c) => c && Array.isArray(c.turns) && c.turns.length) : [];
  } catch { return []; }
}

// 저장 공간이 모자라면 가장 오래된 대화부터 지우며 다시 시도한다. 저장이 막힌 브라우저면 이번 화면에서만 유지
function saveConvs() {
  const kept = [...convs].sort((a, b) => b.updated - a.updated);
  for (;;) {
    try { localStorage.setItem(CONV_KEY, JSON.stringify(kept)); return; }
    catch (e) {
      if (e?.name !== "QuotaExceededError" || kept.length <= 1) return;
      const old = kept.pop();
      convs = convs.filter((c) => c.id !== old.id);
    }
  }
}

// 이어 갈 때 서버에 보내는 최근 턴 (답변은 인용 표시를 뺀 앞부분만)
function threadOf(conv) {
  if (!conv) return [];
  return conv.turns.slice(-THREAD_TURNS).map((t) => ({ question: t.question, answer: t.answer, company: conv.company || null }));
}

function addTurn(r) {
  if (r.route === "clarify") return;                   // 되묻기는 대화에 넣지 않는다 (회사를 고르면 같은 질문을 다시 보낸다)
  const answer = String(r.answer || "").replace(/\[[CTHDL]\d+\]/g, "").slice(0, THREAD_ANSWER_CHARS);
  const { trace, ...result } = r;                      // 처리 기록은 크고 다시 볼 일이 드물어 저장하지 않는다
  if (!active) {
    active = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, company, created: Date.now(), turns: [] };
    convs.push(active);
  }
  active.turns.push({ question: r.question, answer, result });
  active.updated = Date.now();
  convs = convs.sort((a, b) => b.updated - a.updated).slice(0, CONV_MAX);
  saveConvs();
  thread = threadOf(active);
  renderThread();
  renderConvs();
}

function closeConv() {
  active = null;
  thread = [];
  renderThread();
  renderConvs();
}

function openConv(id) {
  const conv = convs.find((c) => c.id === id);
  if (!conv) return;
  active = conv;
  thread = threadOf(conv);
  if (company !== conv.company) selectCompany(conv.company, { keepConv: true });
  $("progress").hidden = true;
  $("error").hidden = true;
  const last = conv.turns[conv.turns.length - 1];
  renderResult({ trace: null, ...last.result }, last.question);
  renderThread();
  renderConvs();
  $("question").value = "";
  $("question").focus();
}

function deleteConv(id) {
  convs = convs.filter((c) => c.id !== id);
  saveConvs();
  if (active?.id === id) closeConv();
  else renderConvs();
}

const convTime = (ms) => {
  const d = new Date(ms), now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return d.toDateString() === now.toDateString() ? `${pad(d.getHours())}:${pad(d.getMinutes())}`
    : `${d.getFullYear() !== now.getFullYear() ? d.getFullYear() + "." : ""}${d.getMonth() + 1}.${d.getDate()}`;
};

// 오른쪽: 모든 회사의 대화를 최근에 이어 간 순서로. 상자마다 회사 이름을 달고, 누르면 그 회사로 바꿔 대화를 이어 간다
function renderConvs() {
  const mine = [...convs].sort((a, b) => b.updated - a.updated).slice(0, CONV_MAX);
  const box = $("convs");
  if (!mine.length) {
    box.innerHTML = `<li class="empty">아직 없습니다. 질문하면 여기에 쌓이고, 눌러서 이어 갈 수 있습니다.</li>`;
    return;
  }
  box.innerHTML = mine.map((x) => {
    const first = x.turns[0].question, last = x.turns[x.turns.length - 1].question;
    return `<li class="conv ${active?.id === x.id ? "active" : ""}">
      <button type="button" class="conv-open" data-id="${esc(x.id)}" title="${esc(x.turns.map((t) => t.question).join("\n"))}"
        aria-current="${active?.id === x.id}">
        <span class="conv-company">${esc(companies.find((c) => c.id === x.company)?.name || "전체")}</span>
        <span class="conv-title">${esc(first)}</span>
        ${x.turns.length > 1 ? `<span class="conv-last">→ ${esc(last)}</span>` : ""}
        <span class="conv-meta">${x.turns.length}턴 · ${convTime(x.updated)}</span>
      </button>
      <button type="button" class="conv-del" data-del="${esc(x.id)}" title="이 대화 지우기" aria-label="대화 지우기">×</button></li>`;
  }).join("");
  box.querySelectorAll(".conv-open").forEach((b) => b.addEventListener("click", () => openConv(b.dataset.id)));
  box.querySelectorAll(".conv-del").forEach((b) => b.addEventListener("click", () => deleteConv(b.dataset.del)));
}

function renderThread() {
  $("thread").hidden = $("new-thread").hidden = !active;
  $("thread").textContent = active ? `대화 이어가는 중 (${active.turns.length}턴)` : "";
}

$("new-thread").addEventListener("click", () => {
  closeConv();
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
