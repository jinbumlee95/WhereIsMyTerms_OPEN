// 이용 안내: 수록 범위 표 (회사 · 문서 수 · 최근 반영일 · 문서별 원문 링크)
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function coverage() {
  const box = document.getElementById("coverage");
  try {
    const companies = await (await fetch("/api/companies")).json();
    box.innerHTML = companies.map((c) => `<details class="ev">
        <summary><span class="head">${esc(c.name)}</span>
          <span class="sub">문서 ${c.documents.length}개 · 최근 반영 ${esc(c.latest_version || "-")}</span></summary>
        <ul class="doclist">${c.documents.map((d) => `<li>${d.source_url
          ? `<a href="${esc(d.source_url)}" target="_blank" rel="noopener">${esc(d.title)}</a>` : esc(d.title)}
          <span class="muted">${esc(d.latest_version)}</span></li>`).join("")}</ul>
      </details>`).join("");
  } catch (e) {
    box.innerHTML = `<p class="muted">수록 범위를 불러오지 못했습니다.</p>`;
  }
}
coverage();
