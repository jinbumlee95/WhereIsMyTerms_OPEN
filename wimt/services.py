"""서비스 메타데이터: 표시 이름, 질문 속 별칭, 플랫폼.

- 표시 이름: 조각 머리말에 넣어 벡터·BM25 가 서비스 이름으로도 찾게 한다 ("[발로란트 · 운영정책] …").
- 별칭 -> 필터: "발로란트 환불"이면 발로란트 문서 + 라이엇 공통 문서(개인정보처리방침, 동의서 등)로 거른다.
- 별칭 -> 우선 문서: "쿠팡이츠", "스팀" 같은 말은 해당 문서를 거르지 않고 순위만 올린다 (다른 문서가 답일 수도 있어서).
- 플랫폼: PUBG 처럼 플랫폼별로 같은 약관이 있는 문서는 경로의 괄호에서 플랫폼을 뽑는다.
"""
import re

# 서비스 -> 표시 이름
NAMES = {
    "naver": "네이버", "navercloud": "네이버 클라우드 플랫폼", "kakao": "카카오",
    "coupang": "쿠팡", "kt": "KT", "tving": "티빙", "toss": "토스", "krafton/pubg": "배틀그라운드(PUBG)",
    "nexon/mabinogimobile": "마비노기 모바일", "riotgames": "라이엇 게임즈",
    "riotgames/leagueoflegends": "리그 오브 레전드", "riotgames/valorant": "발로란트", "riotgames/2xko": "2XKO",
    "riotgames/legendsofruneterra": "레전드 오브 룬테라", "riotgames/wildrift": "와일드 리프트",
    "riotgames/pcbang": "라이엇 PC방", "riotgames/riftbound": "리프트바운드",
}

# 회사(최상위 폴더) -> 표시 이름. 회사를 골라 달라고 되물을 때, 회사 목록을 보여 줄 때
COMPANY_NAMES = {"coupang": "쿠팡", "kt": "KT", "tving": "티빙", "riotgames": "라이엇 게임즈",
                 "naver": "네이버", "navercloud": "네이버 클라우드 플랫폼", "kakao": "카카오", "toss": "토스",
                 "krafton": "크래프톤 (배틀그라운드)", "nexon": "넥슨 (마비노기 모바일)"}


def company_name(company: str) -> str:
    return COMPANY_NAMES.get(company, company)


# 별칭 -> 서비스 (또는 회사). 긴 별칭부터 맞춘다 ("리그 오브 레전드" 가 "레전드" 보다 먼저).
ALIASES = {
    "naver": ["네이버", "naver"],
    "navercloud": ["네이버 클라우드 플랫폼", "네이버클라우드", "네이버 클라우드", "naver cloud", "navercloud", "ncloud"],
    "kakao": ["카카오", "kakao"],
    "coupang": ["쿠팡", "로켓와우", "와우 멤버십", "와우멤버십"],
    "kt": ["kt", "케이티"],
    "tving": ["티빙", "tving"],
    "toss": ["토스", "toss", "비바리퍼블리카"],
    "krafton/pubg": ["배틀그라운드", "배그", "pubg", "펍지", "크래프톤"],
    "nexon/mabinogimobile": ["마비노기 모바일", "마비노기", "마모", "넥슨"],
    "riotgames": ["라이엇 게임즈", "라이엇게임즈", "라이엇", "riot", "tft", "전략적 팀 전투", "롤토체스"],
    "riotgames/leagueoflegends": ["리그 오브 레전드", "리그오브레전드", "롤", "lol", "소환사"],
    "riotgames/valorant": ["발로란트", "valorant", "발로"],
    "riotgames/2xko": ["2xko"],
    "riotgames/legendsofruneterra": ["레전드 오브 룬테라", "룬테라", "lor"],
    "riotgames/wildrift": ["와일드 리프트", "와일드리프트", "와리"],
    "riotgames/pcbang": ["피시방", "pc방", "pc 방"],
    "riotgames/riftbound": ["리프트바운드", "riftbound"],
}

# 별칭 -> 우선할 문서 (경로의 일부). 거르지 않고 순위만 올린다.
DOC_ALIASES = {
    "쿠팡이츠서비스이용기준": ["쿠팡이츠", "배달"],
    "쿠팡플레이서비스이용기준": ["쿠팡플레이"],
    "멤버십서비스이용약관": ["와우", "멤버십"],
    "판매이용약관": ["판매자", "셀러", "입점", "마켓플레이스"],
    "(Steam)": ["스팀", "steam"],
    "(Xbox)": ["엑스박스", "xbox"],
    "(PlayStation)": ["플레이스테이션", "플스", "playstation", "ps4", "ps5"],
    "(EpicGames)": ["에픽", "epic"],
    "(KraftonID웹사이트)": ["크래프톤 id", "크래프톤id", "krafton id"],
}

_PLATFORM = re.compile(r"\(([^)]+)\)\.md$")


def display_name(service: str) -> str:
    return NAMES.get(service, NAMES.get(service.split('/')[0], service))


def platform(path: str) -> str:
    """'krafton/pubg/서비스이용약관(Steam).md' -> 'Steam'."""
    m = _PLATFORM.search(path)
    return m.group(1) if m else ""


def doc_label(c: dict) -> str:
    """조각 머리말용: '배틀그라운드(PUBG) · 서비스 이용약관 (Steam)'."""
    p = platform(c["path"])
    return f"{display_name(c['service'])} · {c.get('doc_title') or ''}" + (f" ({p})" if p else "")


# 한글 별칭 뒤에 오면 다른 말이 되는 글자 ("토스트"는 토스가 아니다)
NOT_FOLLOWED = {"토스": "트"}


def _pat(name: str) -> str:
    # 영문 별칭(kt, lol)은 단어 경계로, 한글은 앞 글자가 한글이 아닐 때만 ("컨트롤"의 "롤" 제외)
    if name.isascii():
        return rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])"
    after = NOT_FOLLOWED.get(name)
    return rf"(?<![가-힣]){re.escape(name)}" + (f"(?![{after}])" if after else "")


def _hit(name: str, q: str) -> bool:
    return re.search(_pat(name), q) is not None


def detect(query: str) -> dict:
    """질문 속 서비스 이름 -> 검색 필터. 하위 서비스면 그 서비스 + 회사 공통 문서, 아니면 회사 전체.
    여러 서비스가 나오면 모두 포함한다 ("롤이랑 발로란트 환불 비교")."""
    q = query.lower()
    found = []
    # Consume longer names first so '네이버 클라우드' does not also select 네이버.
    for name, svc in sorted(((n, s) for s, names in ALIASES.items() for n in names),
                            key=lambda pair: len(pair[0]), reverse=True):
        pat = _pat(name)
        if re.search(pat, q):
            if svc not in found:
                found.append(svc)
            q = re.sub(pat, lambda m: ' ' * len(m.group()), q)
    if not found:
        return {}
    subs = [s for s in found if "/" in s]
    companies = {s.split("/")[0] for s in found}
    if subs and all(s.split("/")[0] in {x.split("/")[0] for x in subs} for s in found):
        # 하위 서비스 + 회사 공통(루트) 문서. 루트 서비스가 없는 회사(krafton 등)는 하위 서비스만 걸린다.
        return {"service": sorted(set(subs) | {s.split("/")[0] for s in subs})}
    return {"company": sorted(companies) if len(companies) > 1 else companies.pop()}


def single_company(query: str) -> str | None:
    """질문이 가리키는 회사가 하나면 그 회사(최상위 폴더), 없거나 여럿이면 None. 추천 질문을 누를 때 회사를 바꾸는 데 쓴다."""
    f = detect(query)
    found = {s.split("/")[0] for s in f.get("service", [])} | set([f["company"]] if isinstance(f.get("company"), str)
                                                                 else f.get("company", []))
    return found.pop() if len(found) == 1 else None


def preferred_docs(query: str) -> list[str]:
    """질문 속 말로 우선할 문서 경로 조각 (예: '쿠팡이츠서비스이용기준', '(Steam)')."""
    q = query.lower()
    return [doc for doc, names in DOC_ALIASES.items() if any(_hit(n, q) for n in names)]
