"""나라장터 데이터 수집.

명령줄 사용 예:
    python collector.py --days 30        # 최근 30일 수집
    python collector.py --from 20260101 --to 20260131
"""
import argparse
import json
from datetime import date, datetime, timedelta

import api_client
import db
from config import load_settings


def g(row: dict, *names, default=None):
    """여러 후보 필드명 중 값이 있는 첫 번째를 반환."""
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v
    return default


def num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def key_of(row: dict) -> str:
    no = g(row, "bidNtceNo", default="")
    ord_ = g(row, "bidNtceOrd", default="000")
    return f"{no}-{ord_}"


def _range(d1: date, d2: date):
    d = d1
    while d <= d2:
        yield d
        d += timedelta(days=1)


def _date_params(op: str, d: date) -> dict:
    s = load_settings()
    return {
        "inqryDiv": s["inqry_div_date"].get(op, "1"),
        "inqryBgnDt": d.strftime("%Y%m%d") + "0000",
        "inqryEndDt": d.strftime("%Y%m%d") + "2359",
    }


# ---------- 행 변환 ----------

def map_bid(r: dict) -> dict:
    return {
        "bid_key": key_of(r),
        "bid_no": g(r, "bidNtceNo"),
        "bid_ord": g(r, "bidNtceOrd"),
        "name": g(r, "bidNtceNm"),
        "org": g(r, "ntceInsttNm"),
        "demand_org": g(r, "dminsttNm"),
        "region": g(r, "cnstrtsiteRgnNm", "prtcptLmtRgnNm"),
        "main_type": g(r, "mainCnsttyNm", "cnsttyNm"),
        "method": g(r, "sucsfbidMthdNm", "bidMethdNm"),
        "notice_dt": g(r, "bidNtceDt", "rgstDt"),
        "close_dt": g(r, "bidClseDt"),
        "open_dt": g(r, "opengDt"),
        "est_price": num(g(r, "presmptPrce")),
        "lower_rate": num(g(r, "sucsfbidLwltRate")),
        "url": g(r, "bidNtceDtlUrl", "bidNtceUrl"),
        "raw": r,
    }


A_VALUE_FIELDS = [  # A값 구성 항목(보험료 등) - 응답에 있는 것만 합산
    "npnInsrprm", "mrfnHealthInsrprm", "rtrfundNon", "odsnLngtrmrcprInsrprm",
    "sftyMngcst", "sftyChckMngcst", "qltyMngcst", "usefulAmt",
]


def map_base(r: dict) -> dict:
    a = sum(num(r.get(f)) or 0 for f in A_VALUE_FIELDS)
    return {
        "bid_key": key_of(r),
        "base_amt": num(g(r, "bssamt", "bsisAmt")),
        "range_begin": num(g(r, "rsrvtnPrceRngBgnRate")),
        "range_end": num(g(r, "rsrvtnPrceRngEndRate")),
        "a_value": a or None,
        "raw": r,
    }


def map_prelim(r: dict) -> dict:
    drawn = str(g(r, "drwtYn", default="")).upper() in ("Y", "1", "TRUE")
    return {
        "bid_key": key_of(r),
        "sno": int(num(g(r, "compnoRsrvtnPrceSno", "rsrvtnPrceSno", default=0)) or 0),
        "price": num(g(r, "bsisPlnprc", "rsrvtnPrce")),
        "drawn": 1 if drawn else 0,
        "draw_count": int(num(g(r, "drwtNum", default=0)) or 0),
        "plan_price": num(g(r, "plnprc")),
        "base_amt": num(g(r, "bssamt")),
        "open_dt": g(r, "rlOpengDt", "opengDt"),
    }


def map_winner(r: dict) -> dict:
    return {
        "bid_key": key_of(r),
        "open_dt": g(r, "rlOpengDt", "opengDt"),
        "participants": int(num(g(r, "prtcptCnum", default=0)) or 0),
        "winner": g(r, "bidwinnrNm"),
        "winner_bizno": g(r, "bidwinnrBizno"),
        "win_amt": num(g(r, "sucsfbidAmt")),
        "win_rate": num(g(r, "sucsfbidRate")),
        "raw": r,
    }


def map_rank(bid_key: str, r: dict) -> dict:
    return {
        "bid_key": bid_key,
        "rank": int(num(g(r, "opengRank", default=0)) or 0),
        "bizno": g(r, "prcbdrBizno", default=""),
        "company": g(r, "prcbdrNm"),
        "amount": num(g(r, "bidprcAmt")),
        "rate": num(g(r, "bidprcrt")),
        "note": g(r, "rmrk", default=""),
    }


# ---------- 수집 ----------

def collect_day(con, op: str, mapper, table: str, d: date) -> int:
    items = api_client.call_all(op, _date_params(op, d))
    n = 0
    for r in items:
        row = mapper(r)
        if not row.get("bid_key") or row["bid_key"].startswith("-"):
            continue
        db.upsert(con, table, row)
        n += 1
    con.commit()
    return n


JOBS = [
    ("bid_list", map_bid, "bids", "입찰공고"),
    ("base_amount", map_base, "base_amounts", "기초금액"),
    ("winners", map_winner, "results", "낙찰결과"),
    ("prelim_prices", map_prelim, "prelim_prices", "예비가격"),
]


def collect(d1: date, d2: date, jobs=None, progress=None) -> dict:
    """기간 수집. progress(text, ratio) 콜백으로 진행상황 전달."""
    con = db.connect()
    jobs = [j for j in JOBS if jobs is None or j[0] in jobs]
    days = list(_range(d1, d2))
    total_steps = max(1, len(days) * len(jobs))
    step, summary = 0, {}
    for op, mapper, table, label in jobs:
        cnt, errs = 0, []
        for d in days:
            step += 1
            if progress:
                progress(f"{label} {d:%Y-%m-%d} 수집 중...", step / total_steps)
            try:
                cnt += collect_day(con, op, mapper, table, d)
            except api_client.ApiError as e:
                errs.append(f"{d}: {e}")
                if "키" in str(e) or "SERVICE_KEY" in str(e) or "등록되지" in str(e):
                    break  # 키 문제면 계속해도 소용없음
        period = f"{d1}~{d2}"
        db.log(con, label, period, cnt, "; ".join(errs[:3]))
        summary[label] = {"count": cnt, "errors": errs}
    con.close()
    return summary


def collect_ranks(bid_no: str, bid_ord: str = "000") -> int:
    """특정 공고의 개찰순위(업체별 투찰금액) 수집."""
    s = load_settings()
    params = {"bidNtceNo": bid_no, "bidNtceOrd": bid_ord}
    div = s["inqry_div_bidno"].get("ranks")
    if div:
        params["inqryDiv"] = div
    items = api_client.call_all("ranks", params)
    con = db.connect()
    key = f"{bid_no}-{bid_ord}"
    for r in items:
        db.upsert(con, "ranks", map_rank(key, r))
    con.commit()
    con.close()
    return len(items)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="나라장터 데이터 수집")
    ap.add_argument("--days", type=int, default=7, help="최근 N일")
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    a = ap.parse_args()
    if a.d1:
        d1 = datetime.strptime(a.d1, "%Y%m%d").date()
        d2 = datetime.strptime(a.d2, "%Y%m%d").date() if a.d2 else date.today()
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=a.days - 1)
    res = collect(d1, d2, progress=lambda t, p: print(f"[{p:5.0%}] {t}", end="\r"))
    print()
    print(json.dumps({k: {"건수": v["count"], "오류": v["errors"][:3]} for k, v in res.items()},
                     ensure_ascii=False, indent=2))
