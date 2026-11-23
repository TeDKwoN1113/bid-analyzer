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


def fetch(begin: date, end: date, company: str = "COM01", key: str | None = None, timeout: int = 60) -> list[dict]:
    key = key or api_key()
    if not key:
        raise KepcoError("한전 API 인증키(KEPCO_API_KEY)가 없습니다.")
    r = requests.get(URL, params={"companyId": company, "noticeBeginDate": begin.strftime("%Y%m%d"),
                                  "noticeEndDate": end.strftime("%Y%m%d"), "apiKey": key,
                                  "returnType": "json"}, timeout=timeout)
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


def refresh_if_stale(hours: float = 3, days: int = 14) -> str | None:
    """사이트를 열 때 한전 공고가 오래됐으면 최근 공고를 새로 받음.
    (GitHub 자동 수집 서버에서는 한전 API 접속이 막혀서, 사이트 서버에서 받아 둠 → 텔레그램 알림도 이 자료 사용)"""
    if not api_key():
        return None
    last = db.get_kv("kepco_fetched_at")
    if last and pd.Timestamp.now() - pd.Timestamp(last) < pd.Timedelta(hours=hours):
        return None
    db.set_kv("kepco_fetched_at", pd.Timestamp.now().isoformat())   # 실패해도 매번 재시도하지 않도록 먼저 기록
    today = date.today()
    rows = [map_row(x) for x in fetch(today - timedelta(days=days), today, timeout=20)]
    rows = [x for x in rows if x["bid_no"]]
    db.upsert_many("kepco_bids", rows)
    return f"한전 공고 {len(rows)}건 갱신"


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
    db.set_kv("kepco_fetched_at", pd.Timestamp.now().isoformat())
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


# ---------------- SRM 투찰 이력 불러오기 ----------------
# 엑셀 칸 이름 추측용 (SRM 내려받기 양식이 바뀌어도 사용자가 화면에서 고를 수 있음)
IMPORT_FIELDS = {
    "bid_no": ("공고번호", ["공고번호", "입찰번호", "공고 번호", "bid"]),
    "name": ("공고명(선택)", ["공고명", "입찰건명", "건명", "공사명"]),
    "base_amt": ("기초금액/예비가격기초금액(선택)", ["예비가격기초", "기초금액", "기초"]),
    "plan_price": ("예정가격", ["예정가격", "예가"]),
    "first_amt": ("1순위(낙찰) 금액", ["1순위", "낙찰금액", "최저", "낙찰가"]),
    "lower_rate": ("낙찰하한율(선택)", ["하한율", "하한"]),
    "participants": ("참가업체 수(선택)", ["참가업체", "참가수", "업체수", "투찰업체"]),
    "our_amount": ("우리 투찰금액(선택)", ["투찰금액", "우리", "당사", "입찰금액"]),
    "our_rank": ("우리 순위(선택)", ["순위"]),
    "open_dt": ("개찰일(선택)", ["개찰일", "개찰일시", "개찰"]),
}


def guess_columns(cols) -> dict:
    """엑셀 칸 이름으로 항목 추측 {항목: 칸이름}."""
    out, used = {}, set()
    for key, (_, words) in IMPORT_FIELDS.items():
        for w in words:
            hit = next((c for c in cols if c not in used and w in str(c).replace(" ", "")), None)
            if hit is not None:
                out[key] = hit
                used.add(hit)
                break
    return out


def import_results(df_in: pd.DataFrame, mapping: dict, default_lr: float = 87.745) -> tuple[int, int]:
    """SRM 이력(엑셀) → kepco_results 저장. 공고가 한전 공고 목록에 없으면 최소 정보로 추가. (저장 건수, 건너뛴 건수)"""
    def col(k):
        c = mapping.get(k)
        return df_in[c] if c in df_in else pd.Series([None] * len(df_in), index=df_in.index)

    def nums(x):
        return pd.to_numeric(x.astype(str).str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")
    no = col("bid_no").astype(str).str.strip()
    plan, first = nums(col("plan_price")), nums(col("first_amt"))
    base, lr = nums(col("base_amt")), nums(col("lower_rate")).fillna(default_lr)
    part, ours, rank = nums(col("participants")), nums(col("our_amount")), nums(col("our_rank"))
    name = col("name")
    opened = pd.to_datetime(col("open_dt"), errors="coerce", format="mixed")
    have = set(db.query_df("SELECT bid_no FROM kepco_bids")["bid_no"])
    res, bids, skipped = [], [], 0
    for i in df_in.index:
        b = no[i]
        if not b or b.lower() in ("nan", "none") or pd.isna(plan[i]) or plan[i] <= 0:
            skipped += 1
            continue
        res.append({"bid_no": b, "plan_price": float(plan[i]),
                    "first_amt": None if pd.isna(first[i]) else float(first[i]),
                    "lower_rate": float(lr[i]), "a_value": 0.0,
                    "participants": None if pd.isna(part[i]) else int(part[i]),
                    "our_amount": None if pd.isna(ours[i]) else float(ours[i]),
                    "our_rank": None if pd.isna(rank[i]) else int(rank[i]),
                    "memo": "SRM 이력 불러오기", "created_at": db.now()})
        if b not in have:
            bids.append({"bid_no": b, "company": "COM01", "name": None if pd.isna(name[i]) else str(name[i]),
                         "item_type": "Construction", "purchase_type": "ConstructionService",
                         "base_amt": None if pd.isna(base[i]) else float(base[i]),
                         "end_dt": None if pd.isna(opened[i]) else opened[i].strftime("%Y-%m-%d %H:%M:%S"),
                         "progress": "Final", "updated_at": db.now()})
        elif not pd.isna(base[i]):
            db.execute("UPDATE kepco_bids SET base_amt = :a WHERE bid_no = :n AND base_amt IS NULL",
                       {"a": float(base[i]), "n": b})
    db.upsert_many("kepco_bids", bids)
    db.upsert_many("kepco_results", res)
    return len(res), skipped


def parse_result_text(text: str) -> dict:
    """SRM '입찰공고 개찰결과' 화면을 통째로 복사한 글에서 숫자 읽기.
    {bid_no, plan_price, lower_rate, bidders:[{rank, company, bizno, amount, rate}], below:[...]}"""
    import re
    t = text.replace("\xa0", " ")
    out = {"bid_no": None, "plan_price": None, "lower_rate": None, "bidders": [], "below": []}
    m = re.search(r"\b([A-Z]\d{9,})\b", t)
    if m:
        out["bid_no"] = m.group(1)

    def num_after(label):
        mm = re.search(r"(?:" + label + r")[^\d\n]{0,20}\n?\s*([\d,]+(?:\.\d+)?)", t)
        return float(mm.group(1).replace(",", "")) if mm else None
    out["plan_price"] = num_after("예정가격")
    out["lower_rate"] = num_after(r"적격률|낙찰하한율|하한율")
    # 적격률 탈락 목록은 따로 (참가업체 수에만 포함)
    parts = re.split(r"적격률\s*탈락", t, maxsplit=1)
    row_re = re.compile(r"(?:^|\n)\s*(\d+)\s*번?\s+(.+?)\s+(\d{3}-\d{2}-\d{5})\s+\S*?\s*([\d,]{5,})\s+(\d{2,3}\.\d+)")
    for m in row_re.finditer(parts[0]):
        out["bidders"].append({"rank": int(m.group(1)), "company": m.group(2).strip(),
                               "bizno": m.group(3).replace("-", ""), "amount": float(m.group(4).replace(",", "")),
                               "rate": float(m.group(5))})
    if len(parts) > 1:
        low_re = re.compile(r"(?:^|\n)\s*(.+?)\s+(\d{3}-\d{2}-\d{5})\s+\S*?\s*([\d,]{5,})\s+(\d{2,3}\.\d+)")
        for m in low_re.finditer(parts[1]):
            out["below"].append({"company": m.group(1).strip(), "bizno": m.group(2).replace("-", ""),
                                 "amount": float(m.group(3).replace(",", "")), "rate": float(m.group(4))})
    return out


def save_parsed_result(p: dict, bizno: str = "") -> str:
    """읽은 개찰결과를 저장 (kepco_results + 업체별 순위는 ranks 에 'KEPCO-공고번호'로)."""
    if not p.get("bid_no") or not p.get("plan_price"):
        raise ValueError("공고번호나 예정가격을 찾지 못했습니다. 화면 전체를 복사했는지 확인하세요.")
    bd = sorted(p["bidders"], key=lambda x: x["rank"])
    first = bd[0]["amount"] if bd else None
    ours = next((x for x in bd + p["below"] if bizno and x["bizno"] == bizno), None)
    db.upsert_many("kepco_results", [{
        "bid_no": p["bid_no"], "plan_price": p["plan_price"], "first_amt": first,
        "lower_rate": p["lower_rate"] or 87.745, "a_value": 0.0,
        "participants": (len(bd) + len(p["below"])) or None,
        "our_amount": ours["amount"] if ours else None, "our_rank": ours.get("rank") if ours else None,
        "memo": "SRM 개찰결과 화면 붙여넣기", "created_at": db.now()}])
    if bd:
        db.upsert_many("ranks", [{"bid_key": "KEPCO-" + p["bid_no"], "rank": x["rank"], "bizno": x["bizno"],
                                  "company": x["company"], "amount": x["amount"], "rate": x["rate"], "note": ""}
                                 for x in bd[:100]])
    have = db.query_df("SELECT base_amt FROM kepco_bids WHERE bid_no = :n", {"n": p["bid_no"]})
    base = have["base_amt"].iloc[0] if len(have) else None
    msg = f"{p['bid_no']}: 예정가격 {p['plan_price']:,.0f}원 · 참가 {len(bd) + len(p['below'])}곳"
    if first:
        msg += f" · 1순위 {first:,.0f}원"
    if base and pd.notna(base):
        msg += f" · 사정율 {p['plan_price'] / base * 100:.3f}%"
    else:
        msg += " · ⚠️ 한전 공고 목록에 기초금액이 없어 사정율 계산 불가 (공고가 1년보다 오래됐거나 번호가 다름)"
    if ours:
        msg += f" · 우리 {ours['amount']:,.0f}원" + (f" ({ours['rank']}위)" if ours.get("rank") else " (적격률 탈락)")
    return msg


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
