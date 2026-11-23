"""한국전력공사(한전) 전자입찰 공고 수집·분석.

전력데이터개방포털(bigdata.kepco.co.kr) '전자입찰 계약정보' API 사용.
  - 인증키: 개방포털 회원가입 → API 인증키 신청 (40자리) → Secrets 의 KEPCO_API_KEY
  - 공고만 제공 (개찰 순위·1순위 금액은 없음). 개찰 후 '사정금액'이 채워지면 사정율 통계를 냄.

    python kepco.py            # 최근 공고 수집
    python kepco.py --check    # 응답 항목 확인
"""
import json
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

import db
from config import load_settings

URL = "https://bigdata.kepco.co.kr/openapi/v1/electContract.do"
MAX_SPAN = 90          # API 한 번에 조회 가능한 공고일 범위(일)
REFRESH_DAYS = 60      # 진행상태·사정금액이 바뀌므로 최근 공고는 매일 다시 받음
BACKFILL_DAYS = 365

COMPANIES = {"COM01": "한국전력공사", "COM02": "한국서부발전", "COM04": "한국남부발전", "COM05": "한국중부발전",
             "COM06": "한국남동발전", "COM08": "한국동서발전", "COM09": "한국전력기술", "COM10": "한전KPS",
             "COM11": "한국전력거래소"}
STATES = {"PreAttendProgress": "공고진행", "AttendProgress": "입찰진행", "Close": "마감", "OpenTimed": "개찰",
          "Final": "공고종료", "Fail": "유찰"}
COMPETITION = {"Open": "일반경쟁", "Destination": "지명경쟁", "Limited": "제한경쟁", "Private": "수의"}
BID_TYPES = {"LimitedLowestPrice": "제한적최저가", "LowestPrice": "최저가", "QualifiedEval": "적격심사",
             "CollectivelyBid": "일괄입찰", "Nego": "협상", "TotalEvalSuccess": "종합심사낙찰제"}

KEYWORDS = ["전기", "배전", "송전", "변전", "지중", "가공", "전력", "케이블", "조명", "태양광", "계량"]


class KepcoError(Exception):
    pass


def api_key() -> str:
    return (load_settings().get("kepco_api_key") or "").strip()


def _num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _dt(x):
    """'20250919170000' → '2025-09-19 17:00:00'"""
    s = re.sub(r"\D", "", str(x or ""))
    if len(s) >= 12:
        s = (s + "00")[:14]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} 00:00:00"
    return None


def fetch(begin: date, end: date, company: str = "COM01", key: str | None = None) -> list[dict]:
    key = key or api_key()
    if not key:
        raise KepcoError("한전 API 인증키(KEPCO_API_KEY)가 없습니다.")
    r = requests.get(URL, params={"companyId": company, "noticeBeginDate": begin.strftime("%Y%m%d"),
                                  "noticeEndDate": end.strftime("%Y%m%d"), "apiKey": key,
                                  "returnType": "json"}, timeout=60)
    text = r.text.strip()
    try:
        data = r.json()
    except ValueError:
        raise KepcoError(f"응답을 읽을 수 없음 (HTTP {r.status_code}): {text[:200]}")
    if isinstance(data, dict) and "data" not in data:
        raise KepcoError(f"API 오류 (HTTP {r.status_code}): {json.dumps(data, ensure_ascii=False)[:300]}")
    rows = data.get("data") if isinstance(data, dict) else data
    return [{(k or "").strip(): v for k, v in x.items()} for x in (rows or []) if isinstance(x, dict)]


def _g(r: dict, *names):
    low = {k.lower(): v for k, v in r.items()}
    for n in names:
        v = r.get(n, low.get(n.lower()))
        if v not in (None, "", "-"):
            return v
    return None


def map_row(r: dict) -> dict:
    files = []
    for i in range(1, 6):
        u, n = _g(r, f"filenlink{i}"), _g(r, f"filename{i}")
        if u:
            files.append([str(n or f"첨부{i}"), str(u)])
    return {
        "bid_no": str(_g(r, "no") or ""),
        "company": str(_g(r, "companyId") or ""),
        "name": _g(r, "name"),
        "purchase_type": _g(r, "purchaseType"),
        "item_type": _g(r, "itemType"),
        "place": _g(r, "placeName"),
        "dept": _g(r, "contractReqDepartmentName"),
        "notice_dt": _dt(_g(r, "noticeDate")),
        "attend_close_dt": _dt(_g(r, "bidAttendReqCloseDatetime")),
        "begin_dt": _dt(_g(r, "beginDatetime")),
        "end_dt": _dt(_g(r, "endDatetime")),
        "progress": _g(r, "progressState"),
        "competition": _g(r, "competitionType"),
        "bid_type": _g(r, "bidType"),
        "bid_type_desc": str(_g(r, "bidTypeDetail") or "")[:500] or None,
        "presumed_price": _num(_g(r, "presumedPrice")),
        "presumed_amount": _num(_g(r, "presumedAmount")),
        "base_amt": _num(_g(r, "estimatedPriceBasicAmount")),
        "approx_amt": _num(_g(r, "approximateAmount")),
        "contract_amt": _num(_g(r, "subcontractBudgetAmount")),
        "restrict_text": str(_g(r, "bidAttendRestrict") or "")[:2000] or None,
        "qualification": str(_g(r, "etc") or "")[:2000] or None,
        "location": str(_g(r, "deliveryLocation") or "")[:300] or None,
        "files": json.dumps(files, ensure_ascii=False) if files else None,
        "updated_at": db.now(),
    }


def _windows(begin: date, end: date):
    d = begin
    while d <= end:
        e = min(d + timedelta(days=MAX_SPAN - 1), end)
        yield d, e
        d = e + timedelta(days=1)


def collect(begin: date, end: date, companies=("COM01",), progress=None) -> int:
    n = 0
    for com in companies:
        for b, e in _windows(begin, end):
            rows = [map_row(x) for x in fetch(b, e, com)]
            rows = [x for x in rows if x["bid_no"]]
            db.upsert_many("kepco_bids", rows)
            n += len(rows)
            if progress:
                progress(f"한전 {COMPANIES.get(com, com)} {b}~{e}: {len(rows):,}건")
    return n


def auto(progress=print) -> str:
    """매일: 최근 60일 공고 새로 받기(진행상태·사정금액 갱신) + 처음 한 번 1년치."""
    if not api_key():
        return "한전 API 인증키 없음 → 건너뜀"
    today = date.today()
    n = collect(today - timedelta(days=REFRESH_DAYS), today, progress=progress)
    if db.get_kv("kepco_backfill") != "done":
        n += collect(today - timedelta(days=BACKFILL_DAYS), today - timedelta(days=REFRESH_DAYS + 1), progress=progress)
        db.set_kv("kepco_backfill", "done")
    msg = f"한전 공고 {n:,}건"
    db.log("한전 공고(자동)", f"~{today}", n, msg)
    return msg


# ---------------- 분석 ----------------
_LOAD_COLS = ("bid_no, company, name, purchase_type, item_type, place, dept, notice_dt, attend_close_dt, "
              "begin_dt, end_dt, progress, competition, bid_type, presumed_price, base_amt, approx_amt, "
              "restrict_text, qualification, location, files")
_load_mem: dict = {}


def load(recent_days: int | None = None) -> pd.DataFrame:
    """한전 공고. recent_days 를 주면 마감이 그 안쪽인 공고 + 결과를 기록한 공고만 (전송량 절약).
    같은 프로세스에서 2분 안에 같은 조건으로 다시 부르면 메모리 값 재사용."""
    import time
    key = (db.url(), recent_days, db.WRITES["n"])
    hit = _load_mem.get(key)
    if hit and time.time() - hit[0] < 120:
        return hit[1].copy()
    if recent_days:
        cut = (pd.Timestamp.now() - pd.Timedelta(days=recent_days)).strftime("%Y-%m-%d")
        df = db.query_df(f"SELECT {_LOAD_COLS} FROM kepco_bids WHERE end_dt >= :c OR attend_close_dt >= :c "
                         "OR bid_no IN (SELECT bid_no FROM kepco_results)", {"c": cut})
    else:
        df = db.query_df(f"SELECT {_LOAD_COLS} FROM kepco_bids")
    df = _prepare(df)
    _load_mem.clear()
    _load_mem[key] = (time.time(), df)
    return df.copy()


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    for c in ("notice_dt", "attend_close_dt", "begin_dt", "end_dt"):
        df[c] = pd.to_datetime(df[c], errors="coerce", format="mixed")
    df["상태"] = df["progress"].map(STATES).fillna(df["progress"])
    df["계약방법"] = df["competition"].map(COMPETITION).fillna(df["competition"])
    df["낙찰방법"] = df["bid_type"].map(BID_TYPES).fillna(df["bid_type"])
    df["발주사"] = df["company"].map(COMPANIES).fillna(df["company"])
    df["sajung"] = (df["approx_amt"] / df["base_amt"] * 100).where(df["base_amt"].notna())
    # 사정금액이 기초금액과 똑같으면 개찰 결과(예정가격)가 아니라 공고 때 값 → 사정율로 쓰지 않음
    same = (df["approx_amt"] - df["base_amt"]).abs() < 1
    df.loc[same | ~df["sajung"].between(90, 110), "sajung"] = None
    # 직접 기록한 개찰 결과(예정가격·1순위 금액)가 있으면 그것으로 사정율·1순위 사정율 계산
    res = db.query_df("SELECT * FROM kepco_results")
    df["winner_sajung"] = np.nan
    df["participants"] = np.nan
    if len(res):
        df = df.merge(res.rename(columns={"plan_price": "r_plan", "first_amt": "r_first", "lower_rate": "r_lr",
                                          "a_value": "r_a", "participants": "r_part", "memo": "r_memo",
                                          "created_at": "r_at"}), on="bid_no", how="left")
        rec = df["r_plan"].notna() & df["base_amt"].notna()
        df.loc[rec, "sajung"] = df.loc[rec, "r_plan"] / df.loc[rec, "base_amt"] * 100
        a = df["r_a"].fillna(0)
        implied = (df["r_first"] - a) / (df["r_lr"] / 100) + a
        df["winner_sajung"] = (implied / df["base_amt"] * 100).where(df["r_first"].notna() & df["r_lr"].notna())
        df["participants"] = df["r_part"]
        df.loc[~df["winner_sajung"].between(90, 110), "winner_sajung"] = np.nan
        df.loc[~df["sajung"].between(90, 110), "sajung"] = np.nan
    return df


def recommend(df: pd.DataFrame, row, region: str | None = None) -> dict | None:
    """기록된 한전 개찰 결과로 사정율별 낙찰확률을 계산해 추천 (나라장터와 같은 방식). 10건 미만이면 None."""
    import analysis
    hist = df[df["sajung"].notna() & df["winner_sajung"].notna()]
    if row is not None and row.get("place") and (hist["place"] == row["place"]).sum() >= 30:
        hist = hist[hist["place"] == row["place"]]
    if analysis.n_windows(hist) < 10:
        return None
    pts = analysis.best_points(analysis.win_curve(hist), top=1)
    if not pts:
        return None
    return {"사정율": round(pts[0]["사정율"], 2), "낙찰확률": pts[0]["낙찰확률"], "n": len(hist)}


_cache: dict = {}


def load_cached() -> pd.DataFrame:
    """같은 실행 안에서 여러 번 부를 때 다시 읽지 않도록."""
    import time
    hit = _cache.get("df")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    df = load()
    _cache["df"] = (time.time(), df)
    return df


def is_construction(df: pd.DataFrame) -> pd.Series:
    """공사만 (이름에 '공사'가 있어도 '용역'이면 제외: 예) 배전공사 현장 안전감시 위탁용역)."""
    name = df["name"].fillna("")
    item = df["item_type"].fillna("")
    service = name.str.contains("용역") | (item == "Service")
    return ~service & ((item == "Construction") |
                       ((df["purchase_type"].fillna("") == "ConstructionService") & name.str.contains("공사")))


SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충청", "충북", "충남",
        "전라", "전북", "전남", "경상", "경북", "경남", "제주"]


def region_scope(df: pd.DataFrame, region: str) -> pd.Series:
    """'local'(우리 지역 언급) / 'national'(지역 제한 없음) / 'other'(다른 지역 제한).

    참가자격·입찰참가자격 문구에 시도 이름이 있으면 지역제한으로 보고, 없으면 전국 공고로 봄."""
    rest = df["restrict_text"].fillna("") + " " + df["qualification"].fillna("")
    allt = df["name"].fillna("") + " " + df["place"].fillna("") + " " + df["dept"].fillna("") + " " + \
        df["location"].fillna("") + " " + rest
    mine = allt.str.contains(region, regex=False)
    other = rest.str.contains("|".join(x for x in SIDO if x not in region), regex=True)
    return pd.Series(np.where(mine, "local", np.where(other, "other", "national")), index=df.index)


def filter_bids(df: pd.DataFrame, region: str | None = None, keywords=None, local_only: bool = False,
                include_national: bool = True) -> pd.DataFrame:
    """공사 + 업종 키워드 (+ 참가 가능 지역: 우리 지역 공고, 그리고 지역 제한 없는 전국 공고)."""
    if df.empty:
        return df
    m = is_construction(df)
    kw = keywords or KEYWORDS
    m &= df["name"].fillna("").str.contains("|".join(map(re.escape, kw)), regex=True)
    if local_only and region:
        sc = region_scope(df, region)
        m &= (sc == "local") | ((sc == "national") & include_national)
    out = df[m]
    if region and len(out):
        out = out.assign(지역=region_scope(out, region).map({"local": region, "national": "전국", "other": "타지역"}))
    return out


def is_local(row, region: str) -> bool:
    text = " ".join(str(row.get(c) or "") for c in ("name", "place", "dept", "restrict_text", "location"))
    return bool(region) and region in text


def sajung_stats(df: pd.DataFrame, row=None, region: str | None = None) -> dict | None:
    """과거 한전 공사 공고의 실제 사정율(예정가격/예비가격기초금액) 분포. 비슷한 공고(같은 사업소 → 지역 → 전체) 우선."""
    done = df[df["sajung"].notna() & is_construction(df)]
    if done.empty:
        return None
    groups = []
    if row is not None and row.get("place"):
        groups.append((f"같은 사업소({row['place']})", done[done["place"] == row["place"]]))
    if region:
        loc = done[(done["name"].fillna("") + " " + done["place"].fillna("") + " " + done["dept"].fillna(""))
                   .str.contains(region, regex=False)]
        groups.append((f"{region} 공고", loc))
    groups.append(("한전 공사 전체", done))
    for label, g in groups:
        if len(g) >= 20:
            s = g["sajung"]
            return {"기준": label, "n": len(g), "중앙": float(s.median()), "q25": float(s.quantile(.25)),
                    "q75": float(s.quantile(.75)), "평균": float(s.mean())}
    s = done["sajung"]
    return {"기준": "한전 공사 전체(데이터 적음)", "n": len(done), "중앙": float(s.median()),
            "q25": float(s.quantile(.25)), "q75": float(s.quantile(.75)), "평균": float(s.mean())}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        rows = fetch(date.today() - timedelta(days=7), date.today())
        print(f"{len(rows)}건")
        if rows:
            print(json.dumps(rows[0], ensure_ascii=False, indent=1)[:3000])
    else:
        print(auto())
