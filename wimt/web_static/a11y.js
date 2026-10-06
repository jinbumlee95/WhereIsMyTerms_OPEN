// 화면 설정 (접근성): 밝기(자동·밝게·어둡게), 색약 지원 색, 스크린리더 지원(진행 단계 안내·답으로 초점 이동·초점 강조, 소리는 사용자의 스크린리더가 낸다).
// 설정은 이 브라우저의 localStorage(wimt_a11y)에만 저장하고 서버는 읽지 않는다. <head> 에서 읽어 첫 화면부터 적용한다.
(function () {
  const KEY = "wimt_a11y";
  const THEMES = ["auto", "light", "dark"];
  const THEME_TEXT = { auto: "자동", light: "밝게", dark: "어둡게" };
  const root = document.documentElement;
  const s = { theme: "auto", cvd: false, sr: false };
  try { Object.assign(s, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch { /* 저장이 막힌 브라우저: 기본값 */ }
  if (!THEMES.includes(s.theme)) s.theme = "auto";

  function apply() {
    if (s.theme === "auto") root.removeAttribute("data-theme"); else root.dataset.theme = s.theme;
    if (s.cvd) root.dataset.cvd = "on"; else root.removeAttribute("data-cvd");
    if (s.sr) root.dataset.sr = "on"; else root.removeAttribute("data-sr");
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* 이번 화면에서만 유지 */ } }
  apply();

  // 스크린리더 안내 (aria-live). 스크린리더 지원 모드면 단계마다, 아니면 결과·오류처럼 중요한 것만 부른다
  let live = null;
  function announce(text, { always = false } = {}) {
    if (!always && !s.sr) return;
    if (!live) {
      live = document.createElement("div");
      live.className = "sr-only";
      live.setAttribute("aria-live", "polite");
      live.setAttribute("role", "status");
      document.body.appendChild(live);
    }
    live.textContent = "";
    setTimeout(() => { live.textContent = text; }, 50);      // 같은 문장이어도 다시 읽게 비웠다가 넣는다
  }
  window.wimtA11y = { announce, get sr() { return s.sr; } };

  function controls() {
    const nav = document.querySelector(".tabs");
    if (!nav) return;
    const box = document.createElement("div");
    box.className = "a11y";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "화면 설정");
    box.innerHTML = `<button type="button" data-a11y="theme"></button>
      <button type="button" data-a11y="cvd" title="빨강·초록 대신 파랑·주황으로 구분하고, 색 말고도 밑줄·취소선으로 표시합니다">색약 지원</button>
      <button type="button" data-a11y="sr" title="켜 둔 스크린리더(내레이터·NVDA·VoiceOver 등)가 진행 단계를 읽게 하고, 답이 나오면 답으로 초점을 옮기며, 키보드 초점을 굵게 표시합니다. 소리를 직접 내지는 않습니다">스크린리더 지원</button>`;
    nav.after(box);
    const theme = box.querySelector("[data-a11y=theme]");
    const render = () => {
      theme.textContent = `화면: ${THEME_TEXT[s.theme]}`;
      theme.setAttribute("aria-label", `화면 밝기 ${THEME_TEXT[s.theme]}. 누르면 바뀝니다`);
      box.querySelector("[data-a11y=cvd]").setAttribute("aria-pressed", String(s.cvd));
      box.querySelector("[data-a11y=sr]").setAttribute("aria-pressed", String(s.sr));
    };
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-a11y]");
      if (!b) return;
      const k = b.dataset.a11y;
      if (k === "theme") s.theme = THEMES[(THEMES.indexOf(s.theme) + 1) % THEMES.length];
      else s[k] = !s[k];
      apply(); save(); render();
      announce(k === "theme" ? `화면 밝기 ${THEME_TEXT[s.theme]}`
        : `${k === "cvd" ? "색약 지원" : "스크린리더 지원"} ${s[k] ? "켬" : "끔"}`, { always: true });
    });
    render();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", controls);
  else controls();
})();
