"""설정 파일: API 주소, 수집 조건 등.

조달청 API 주소나 파라미터가 바뀌면 이 파일(또는 settings.json)만 고치면 됩니다.
"""
import json
import os
import time
from urllib.parse import unquote

# 서버(미국 시간)에서 돌아가도 한국 시간 기준으로 날짜 계산
os.environ.setdefault("TZ", "Asia/Seoul")
if hasattr(time, "tzset"):
    time.tzset()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "bids_v2.db")
SAMPLE_DB_PATH = os.path.join(BASE_DIR, "data", "sample.db")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# 조달청 OpenAPI (공공데이터포털). 2025년 개편 이후 주소 기준.
BID_SERVICE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
SCSBID_SERVICE = "https://apis.data.go.kr/1230000/as/ScsbidInfoService"

OPERATIONS = {
    # 입찰공고 (공사)
    "bid_list": f"{BID_SERVICE}/getBidPblancListInfoCnstwk",
    # 입찰공고 기초금액 (공사)
    "base_amount": f"{BID_SERVICE}/getBidPblancListInfoCnstwkBsisAmount",
    # 개찰결과 목록 (공사) - 개찰 직후 참가업체수, 1순위 업체
    "openings": f"{SCSBID_SERVICE}/getOpengResultListInfoCnstwk",
    # 낙찰자 목록 (공사) - 적격심사 후 최종 낙찰자
    "winners": f"{SCSBID_SERVICE}/getScsbidListSttusCnstwk",
    # 예비가격 상세 (공사) - 15개 예비가격, 추첨여부, 예정가격
    "prelim_prices": f"{SCSBID_SERVICE}/getOpengResultListInfoCnstwkPreparPcDetail",
    # 개찰순위 (업체별 투찰금액)
    "ranks": f"{SCSBID_SERVICE}/getOpengResultListInfoOpengCompt",
}

DEFAULT_SETTINGS = {
    "service_key": "",
    "app_password": "",
    # 영구 저장용 DB 주소 (Neon 등 PostgreSQL). 비우면 내 컴퓨터의 data/bids.db 사용
    "database_url": "",
    # 날짜로 조회할 때의 inqryDiv 값 (서비스마다 의미가 다름, 'API 점검' 화면에서 확인 가능)
    "inqry_div_date": {
        "bid_list": "1",
        "base_amount": "1",
        "openings": "1",
        "winners": "1",
        "prelim_prices": "1",
    },
    # 공고번호로 조회할 때의 inqryDiv 값
    "inqry_div_bidno": {
        "ranks": "",          # 개찰순위는 공고번호만으로 조회 (inqryDiv 없음)
        "prelim_prices": "2",
        "base_amount": "2",
    },
    # 한 번에 받는 건수 (999가 안 되면 자동으로 100으로 내려서 재시도)
    "num_of_rows": 999,
    # 자동 수집: 과거 몇 일치까지 채울지, 한 번 실행에 최대 몇 일씩 채울지
    "backfill_days": 365,
    "backfill_per_run": 60,
    # 분석 대상 업종 키워드 (공고명/주공종명에 포함되면 대상). 나중에 '통신', '소방' 등 추가.
    "target_keywords": ["전기"],
    # 기본 필터
    "default_regions": ["울산"],
    "max_amount": 4_000_000_000,   # 40억
}


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    # 웹사이트(Streamlit Cloud)에서는 'Secrets'에 넣은 값을 우선 사용
    for env_name, field in (("DATA_GO_KR_KEY", "service_key"), ("APP_PASSWORD", "app_password"),
                            ("DATABASE_URL", "database_url")):
        val = os.environ.get(env_name)
        if not val:
            try:
                import streamlit as st
                val = st.secrets.get(env_name)
            except Exception:  # secrets 파일이 없으면 무시
                val = None
        if val:
            s[field] = str(val)
    # 인코딩된 키(%2B 등 포함)를 붙여넣어도 동작하도록 디코딩
    if s.get("service_key") and "%" in s["service_key"]:
        s["service_key"] = unquote(s["service_key"])
    return s


def save_settings(s: dict) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
