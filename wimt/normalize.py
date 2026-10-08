"""정규화: "가짜 변경"을 없애기 위한 비교용 텍스트. 원문은 바꾸지 않고, 비교·해시에만 쓴다.

규칙 번호는 docs/정제규칙서.md 와 같다.
"""
import hashlib
import re

# N2 공백 통일: NBSP, 전각 공백, 폭 없는 공백, 한글 채움 문자(ㅤ) 등
_SPACES = re.compile(r"[  -​  　ㅤ﻿\t ]+")
# N4 페이지 동적 요소 (수집 시각, 조회수, 인쇄 버튼 등). 약관 본문의 시행일·부칙은 실제 내용이므로 지우지 않는다.
_DYNAMIC = [
    re.compile(r"^(최종\s*)?(수정|업데이트|조회)\s*(일시|시각|수)\s*[:：].*$"),
    re.compile(r"^(인쇄하기|목록|맨\s*위로|top)$", re.IGNORECASE),
]
# N5 문장부호 통일
_PUNCT = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "ㆍ": "·", "・": "·", "∙": "·", "–": "-", "—": "-"})
# 항 번호(①…⑳, (1), 1.) 접두어: 조항 대응(번호가 밀린 항 찾기)에 쓴다
_MARKER = re.compile(r"^\s*([①-⑳]|\(\d+\)|\[\d+\]|\d+\.)\s*")


def normalize(text: str) -> str:
    """N1~N5 를 적용한 비교용 텍스트."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")                      # N1 줄바꿈 통일
    lines = []
    for line in text.split("\n"):
        line = _SPACES.sub(" ", line).strip()                                  # N2 공백 통일
        line = re.sub(r"^#{1,6}\s+", "", line)                                 # N3 마크다운 표시 제거
        line = re.sub(r"\s*\|\s*", " | ", line) if line.startswith("|") else line
        if not line or any(p.match(line) for p in _DYNAMIC):                   # N4 동적 요소 제거
            continue
        lines.append(line.translate(_PUNCT))                                   # N5 문장부호 통일
    return "\n".join(lines)


def strip_marker(text: str) -> str:
    """조항 대응용 비교 키.

    항 번호를 뗀다 (⑦ → ⑧ 처럼 번호만 밀린 항을 같은 항으로 보기 위해).
    N6 공백을 모두 뺀다 (`제 10조` → `제10조` 같은 띄어쓰기만 바뀐 조항을 같은 조항으로 보기 위해).
    """
    return re.sub(r"\s+", "", _MARKER.sub("", normalize(text), count=1))


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
