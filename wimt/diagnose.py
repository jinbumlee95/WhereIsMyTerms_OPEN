"""첫 진단 (제안서 1단계): 약관 페이지를 일반 HTTP 로 요청하고 robots.txt 를 확인해 수집 방식을 분류한다.

분류
- HTTP 수집: 200 응답이고 정적 HTML 안에 본문 문장이 있다
- 브라우저 수집: 200 응답이지만 본문이 스크립트로 채워진다 (정적 HTML 에 본문 문장이 없음)
- 차단(대체 경로): robots.txt 가 막거나, 403/429 등으로 거절되거나, 요청이 실패한다

봇 탐지 우회는 하지 않는다. 한 URL 당 페이지 1회 + robots.txt 1회만 요청한다.
"""
import html
import re
import time
import urllib.robotparser
from urllib.parse import urlsplit

from .normalize import normalize

USER_AGENT = "WhereIsMyTerms-diagnose/0.1 (personal research; one request per page)"


def _marker(body: str) -> str | None:
    """정적 HTML 에 있는지 볼 본문 문장: 제목이 아닌 첫 긴 문장의 앞 25자."""
    for line in normalize(body).split("\n"):
        if len(line) >= 40 and not line.startswith("|"):
            return re.sub(r"\s+", "", line)[:25]
    return None


def _visible_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = html.unescape(re.sub(r"(?s)<[^>]+>", " ", page))
    return re.sub(r"\s+", "", page)


def check(client, url: str, body: str, robots_cache: dict) -> dict:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = client.get(base + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            robots_cache[base] = (rp, r.status_code)
        except Exception as e:  # robots.txt 를 못 읽으면 허용으로 보지 않고 기록만 남긴다
            robots_cache[base] = (None, f"error: {type(e).__name__}")
    rp, robots_status = robots_cache[base]
    allowed = rp.can_fetch(USER_AGENT, url) if rp else None
    row = {"url": url, "robots_status": robots_status, "robots_allowed": allowed}
    if allowed is False:
        return {**row, "status": None, "static_has_body": None, "method": "차단(대체 경로)", "note": "robots.txt 불허"}
    try:
        r = client.get(url)
    except Exception as e:
        return {**row, "status": None, "static_has_body": None, "method": "차단(대체 경로)", "note": type(e).__name__}
    row["status"] = r.status_code
    if r.status_code != 200:
        return {**row, "static_has_body": None, "method": "차단(대체 경로)", "note": f"HTTP {r.status_code}"}
    marker = _marker(body)
    has = bool(marker) and marker in _visible_text(r.text)
    return {**row, "static_has_body": has, "method": "HTTP 수집" if has else "브라우저 수집",
            "note": "" if marker else "비교할 본문 문장 없음"}


def diagnose(sources: list[dict], delay: float = 1.0) -> list[dict]:
    """sources: [{"url", "body", ...}] (같은 URL 은 한 번만)."""
    import httpx

    rows, robots_cache, seen = [], {}, set()
    # br/zstd 는 추가 패키지 없이는 풀지 못해 요청하지 않는다 (DecodingError 를 '차단'으로 오판하지 않도록)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
        for s in sources:
            if not s["url"] or s["url"] in seen:
                continue
            seen.add(s["url"])
            rows.append({"service": s["service"], "doc": s["path"], **check(client, s["url"], s["body"], robots_cache)})
            time.sleep(delay)
    return rows
