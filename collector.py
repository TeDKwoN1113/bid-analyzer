"""나라장터 데이터 수집.

명령줄 사용 예:
    python collector.py --days 7                    # 최근 7일
    python collector.py --from 20260101 --to 20260131
    python collector.py --auto                      # 매일 자동 수집용: 최근 3일 + 과거 데이터 채우기
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


def _date_params(op: str, d: date) -> dict:
    s = load_settings()
    return {
        "inqryDiv": s["inqry_div_date"].get(op, "1"),
        "inqryBgnDt": d.strftime("%Y%m%d") + "0000",
        "inqryEndDt": d.strftime("%Y%m%d") + "2359",
    }


# ---------- 행 변환 ----------

def map_bid(r: dict) -> list[tuple[str, dict]]:
    return [("bids", {
        "bid_key": key_of(r),
        "bid_no": g(r, "bidNtceNo"),
        "bid_ord": g(r, "bidNtceOrd"),
        "name": g(r, "bidNtceNm"),
        "org": g(r, "ntceInsttNm"),
        "demand_org": g(r, "dminsttNm"),
        "region": g(r, "cnstrtsiteRgnNm", "prtcptLmtRgnNm"),
        "main_type": g(r, "mainCnsttyNm", "cnsttyNm"),
        "method": g(r, "sucsfbidMthdNm", "bidMethdNm"),
        "contract_method": g(r, "cntrctCnclsMthdNm"),
        "notice_dt": g(r, "bidNtceDt", "rgstDt"),
        "close_dt": g(r, "bidClseDt"),
        "open_dt": g(r, "opengDt"),
        "est_price": num(g(r, "presmptPrce")),
        "lower_rate": num(g(r, "sucsfbidLwltRate")),
        "url": g(r, "bidNtceDtlUrl", "bidNtceUrl"),
    })]


def _is_cnstwk(r: dict) -> bool:
    """참가지역·면허제한 API는 물품·용역도 섞여 오므로 공사만 저장."""
    div = g(r, "bsnsDivNm", default="공사")
    return "공사" in str(div)


def map_region(r: dict):
    name = g(r, "prtcptPsblRgnNm", "rgnNm", "prtcptLmtRgnNm")
    return [("regions", {"bid_key": key_of(r), "region": name})] if name and _is_cnstwk(r) else []


def map_license(r: dict):
    # 면허명 + 허용업종 목록(이 중 하나만 있어도 참가 가능)
    name = g(r, "lcnsLmtNm", "indstrytyNm", "lcnsNm")
    extra = g(r, "permsnIndstrytyList", default="")
    text = " ".join(x for x in (name, extra) if x)
    return [("licenses", {"bid_key": key_of(r), "license": text[:2000]})] if text and _is_cnstwk(r) else []


# A값 구성 항목 (공고의 'A값 적용여부'가 Y일 때만 적용)
A_VALUE_FIELDS = {
    "npnInsrprm": "국민연금보험료",
    "mrfnHealthInsrprm": "국민건강보험료",
    "odsnLngtrmrcprInsrprm": "노인장기요양보험료",
    "rtrfundNon": "퇴직공제부금비",
    "sftyMngcst": "산업안전보건관리비",
    "sftyChckMngcst": "안전관리비",
    "qltyMngcst": "품질관리비",        # qltyMngcstAObjYn = Y 일 때만
}


def map_base(r: dict):
    apply_a = str(g(r, "bidPrceCalclAYn", default="Y")).upper() != "N"
    detail = {}
    for f, label in A_VALUE_FIELDS.items():
        if f == "qltyMngcst" and str(g(r, "qltyMngcstAObjYn", default="Y")).upper() == "N":
            continue
        v = num(r.get(f)) or 0
        if v:
            detail[label] = v
    a = sum(detail.values()) if apply_a else 0
    return [("base_amounts", {
        "bid_key": key_of(r),
        "base_amt": num(g(r, "bssamt", "bsisAmt")),
        "range_begin": num(g(r, "rsrvtnPrceRngBgnRate")),
        "range_end": num(g(r, "rsrvtnPrceRngEndRate")),
        "a_value": a or None,
        "a_apply": 1 if apply_a else 0,
        "pure_cost": num(g(r, "bssAmtPurcnstcst")),
        "a_detail": json.dumps(detail, ensure_ascii=False) if detail else None,
    })]


def map_prelim(r: dict):
    drawn = str(g(r, "drwtYn", default="")).upper() in ("Y", "1", "TRUE")
    key = key_of(r)
    return [
        ("prelim_prices", {
            "bid_key": key,
            "sno": int(num(g(r, "compnoRsrvtnPrceSno", "rsrvtnPrceSno", default=0)) or 0),
            "price": num(g(r, "bsisPlnprc", "rsrvtnPrce")),
            "drawn": 1 if drawn else 0,
            "draw_count": int(num(g(r, "drwtNum", default=0)) or 0),
        }),
        ("plans", {
            "bid_key": key,
            "plan_price": num(g(r, "plnprc")),
            "base_amt": num(g(r, "bssamt")),
            "open_dt": g(r, "rlOpengDt", "opengDt"),
        }),
    ]


def map_opening(r: dict):
    # opengCorpInfo = "업체명^사업자번호^대표자^투찰금액^투찰률"
    parts = str(g(r, "opengCorpInfo", default="")).split("^")
    parts += [""] * (5 - len(parts))
    return [("openings", {
        "bid_key": key_of(r),
        "open_dt": g(r, "opengDt", "rlOpengDt"),
        "participants": int(num(g(r, "prtcptCnum", default=0)) or 0),
        "status": g(r, "progrsDivCdNm"),
        "first_company": parts[0] or None,
        "first_bizno": parts[1] or None,
        "first_amt": num(parts[3]),
        "first_rate": num(parts[4]),
    })]


def map_winner(r: dict):
    return [("results", {
        "bid_key": key_of(r),
        "open_dt": g(r, "rlOpengDt", "opengDt"),
        "participants": int(num(g(r, "prtcptCnum", default=0)) or 0),
        "winner": g(r, "bidwinnrNm"),
        "winner_bizno": g(r, "bidwinnrBizno"),
        "win_amt": num(g(r, "sucsfbidAmt")),
        "win_rate": num(g(r, "sucsfbidRate")),
    })]


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


# (API 이름, 변환함수, 화면 표시 이름)
JOBS = [
    ("bid_list", map_bid, "입찰공고"),
    ("regions", map_region, "참가지역"),
    ("licenses", map_license, "면허제한"),
    ("base_amount", map_base, "기초금액"),
    ("openings", map_opening, "개찰결과"),
    ("prelim_prices", map_prelim, "예비가격"),
    ("winners", map_winner, "낙찰자"),
]
LABEL = {j[0]: j[2] for j in JOBS}


# ---------- 수집 ----------

def collect_day(op: str, mapper, d: date) -> int:
    items = api_client.call_all(op, _date_params(op, d))
    batches: dict[str, list] = {}
    for r in items:
        for table, row in mapper(r):
            if not row.get("bid_key") or row["bid_key"].startswith("-"):
                continue
            batches.setdefault(table, []).append(row)
    for table, rows in batches.items():
        db.upsert_many(table, rows)
    db.mark_day(op, d.isoformat(), len(items))
    return len(items)


def collect_days(op: str, days: list[date], progress=None, step0=0, total=1) -> dict:
    cnt, errs, stopped = 0, [], ""
    for i, d in enumerate(days):
        if progress:
            progress(f"{LABEL[op]} {d:%Y-%m-%d} 수집 중...", (step0 + i + 1) / total)
        try:
            cnt += collect_day(op, JOBS_MAP[op], d)
        except api_client.QuotaError as e:
            stopped = f"오늘 호출 한도 초과 → 내일 이어서 수집 ({e})"
            break
        except api_client.KeyError_ as e:
            stopped = str(e)
            break
        except api_client.ApiError as e:
            errs.append(f"{d}: {e}")
    return {"count": cnt, "days": len(days), "errors": errs, "stopped": stopped}


JOBS_MAP = {j[0]: j[1] for j in JOBS}


def collect(d1: date, d2: date, jobs=None, progress=None) -> dict:
    """기간 수집. progress(text, ratio) 콜백으로 진행상황 전달."""
    days = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    ops = [j[0] for j in JOBS if jobs is None or j[0] in jobs]
    total = max(1, len(days) * len(ops))
    summary = {}
    for n, op in enumerate(ops):
        res = collect_days(op, days, progress, n * len(days), total)
        db.log(LABEL[op], f"{d1}~{d2}", res["count"],
               "; ".join(([res["stopped"]] if res["stopped"] else []) + res["errors"][:3]))
        summary[LABEL[op]] = res
    return summary


def auto(progress=print) -> dict:
    """매일 자동 수집: 최근 3일은 항상 새로 받고, 아직 안 받은 과거 날짜를 최신순으로 채움."""
    s = load_settings()
    today = date.today()
    recent = [today - timedelta(days=i) for i in range(2, -1, -1)]
    oldest = today - timedelta(days=s["backfill_days"])
    summary = {}
    for op, _, label in JOBS:
        done = db.done_days(op)
        missing = []
        d = today - timedelta(days=3)
        while d >= oldest and len(missing) < s["backfill_per_run"]:
            if d.isoformat() not in done:
                missing.append(d)
            d -= timedelta(days=1)
        days = recent + missing
        res = collect_days(op, days)
        msg = f"최근 3일 + 과거 {len(missing)}일"
        if res["stopped"]:
            msg += " / " + res["stopped"]
        if res["errors"]:
            msg += " / 오류: " + "; ".join(res["errors"][:3])
        db.log(label + "(자동)", f"{days[-1] if missing else recent[0]}~{today}", res["count"], msg)
        summary[label] = res
        if progress:
            progress(f"{label}: {res['count']:,}건, {msg}")
    r = collect_ranks_auto()
    summary["개찰순위"] = r
    summary["우리 투찰"] = update_my_bids(progress)
    if progress:
        progress(f"개찰순위: 공고 {r['bids']}건 / 업체 {r['rows']:,}행 {r['stopped']}")
    return summary


def quick(progress=print) -> dict:
    """낮 시간 빠른 수집: 어제·오늘 공고, 기초금액, 개찰결과만."""
    today = date.today()
    days = [today - timedelta(days=1), today]
    summary = {}
    for op in ("bid_list", "regions", "licenses", "base_amount", "openings"):
        res = collect_days(op, days)
        summary[LABEL[op]] = res
        if progress:
            progress(f"{LABEL[op]}: {res['count']:,}건 {res['stopped']}")
    summary["우리 투찰"] = update_my_bids(progress)
    db.log("빠른 수집", f"{days[0]}~{today}", sum(v["count"] for v in summary.values() if isinstance(v, dict)),
           "; ".join(v["stopped"] for v in summary.values() if isinstance(v, dict) and v["stopped"]))
    return summary


def collect_ranks(bid_no: str, bid_ord: str = "000", top_n: int | None = None) -> int:
    """특정 공고의 개찰순위(업체별 투찰금액) 수집. top_n 이 있으면 상위 N개 업체만."""
    s = load_settings()
    params = {"bidNtceNo": bid_no, "bidNtceOrd": bid_ord}
    div = s["inqry_div_bidno"].get("ranks")
    if div:
        params["inqryDiv"] = div
    if top_n:
        items, _ = api_client.call("ranks", params, page=1, rows=top_n)
    else:
        items = api_client.call_all("ranks", params)
    key = f"{bid_no}-{bid_ord}"
    rows = [map_rank(key, r) for r in items]
    if top_n:
        rows = sorted((r for r in rows if r["rank"] > 0), key=lambda r: r["rank"])[:top_n]
    # 순위 정보가 없는 공고(유찰 등)도 다시 조회하지 않도록 빈 표시를 남김
    db.upsert_many("ranks", rows or [{"bid_key": key, "rank": 0, "bizno": "", "company": None,
                                      "amount": None, "rate": None, "note": "순위없음"}])
    return len(rows)


def pending_rank_bids(limit: int) -> list[tuple[str, str]]:
    """개찰순위를 아직 안 받은 관심 공고 (업종·지역 키워드, 최신순)."""
    s = load_settings()
    since = (date.today() - timedelta(days=s["rank_lookback_days"])).isoformat()
    import analysis  # 순환 import 방지
    df = analysis.load_dataset()
    if df.empty:
        return []
    done = set(db.query_df("SELECT DISTINCT bid_key FROM ranks")["bid_key"])
    df = df[(df["participants"] > 0) & (df["open_dt"].astype(str) >= since) & ~df["bid_key"].isin(done)]
    df = analysis.apply_filters(df, keywords=analysis.company_keywords(), regions=s["default_regions"],
                                eligible_only=True)
    df = df.sort_values("open_date", ascending=False).head(limit)
    return list(zip(df["bid_no"], df["bid_ord"]))


def update_my_bids(progress=None) -> str:
    """우리 회사 투찰 결과 채우기.

    1) 기록한 공고가 개찰되면 개찰순위 전체를 받아 우리 사업자번호의 순위를 찾음
    2) 이미 받은 개찰순위에 우리 회사가 있으면 투찰 기록에 자동 추가
    """
    c = db.company()
    if not c["bizno"]:
        return "회사 사업자번호 미등록 → 건너뜀"
    mb = db.query_df("SELECT bid_key FROM my_bids WHERE my_rank IS NULL")
    opened = set(db.query_df("SELECT bid_key FROM openings WHERE participants > 0")["bid_key"])
    found, checked, stopped = 0, 0, ""
    for key in mb["bid_key"]:
        if key not in opened:
            continue
        no, ord_ = key.rsplit("-", 1)
        try:
            collect_ranks(no, ord_)          # 전체 순위
        except api_client.QuotaError as e:
            stopped = str(e)
            break
        except api_client.ApiError:
            continue
        mine = db.query_df("SELECT rank, amount FROM ranks WHERE bid_key = :k AND bizno = :b",
                           {"k": key, "b": c["bizno"]})
        rank = int(mine["rank"].iloc[0]) if len(mine) else 0
        db.execute("UPDATE my_bids SET my_rank = :r, checked_at = :t WHERE bid_key = :k",
                   {"r": rank, "t": db.now(), "k": key})
        checked += 1
        found += rank > 0
    # 자동 발견: 개찰순위에 우리 회사가 있는데 기록이 없는 공고
    auto = db.query_df(
        """SELECT k.bid_key, k.rank, k.amount FROM ranks k
           WHERE k.bizno = :b AND NOT EXISTS (SELECT 1 FROM my_bids m WHERE m.bid_key = k.bid_key)""",
        {"b": c["bizno"]})
    if len(auto):
        db.upsert_many("my_bids", [{"bid_key": r.bid_key, "amount": r.amount, "sajung": None,
                                    "memo": "개찰순위에서 자동 발견", "my_rank": int(r.rank),
                                    "checked_at": db.now(), "created_at": db.now()}
                                   for r in auto.itertuples()])
    msg = f"결과 확인 {checked}건(순위 찾음 {found}) · 자동 발견 {len(auto)}건 {stopped}"
    db.log("우리 투찰", "-", checked + len(auto), msg)
    if progress:
        progress(msg)
    return msg


def collect_ranks_auto(progress=None) -> dict:
    s = load_settings()
    todo = pending_rank_bids(s["rank_max_bids_per_run"])
    n, rows, stopped, errs = 0, 0, "", []
    for i, (no, ord_) in enumerate(todo):
        if progress:
            progress(f"개찰순위 {no} ({i + 1}/{len(todo)})", (i + 1) / max(1, len(todo)))
        try:
            rows += collect_ranks(no, ord_ or "000", s["rank_top_n"])
            n += 1
        except api_client.QuotaError as e:
            stopped = f"오늘 호출 한도 초과 → 내일 이어서 ({e})"
            break
        except api_client.KeyError_ as e:
            stopped = str(e)
            break
        except api_client.ApiError as e:
            errs.append(f"{no}: {e}")
    msg = f"관심 공고 {n}/{len(todo)}건"
    if stopped:
        msg += " / " + stopped
    if errs:
        msg += " / 오류: " + "; ".join(errs[:3])
    db.log("개찰순위(자동)", "-", rows, msg)
    return {"bids": n, "rows": rows, "todo": len(todo), "stopped": stopped, "errors": errs}


def backfill_status() -> list[dict]:
    """API별로 며칠치가 수집되어 있는지."""
    s = load_settings()
    df = db.query_df("SELECT op, COUNT(*) AS n, MIN(day) AS first, MAX(day) AS last "
                     "FROM collect_days GROUP BY op")
    info = {r.op: r for r in df.itertuples()}
    out = []
    for op, _, label in JOBS:
        r = info.get(op)
        out.append({"항목": label, "수집된 날수": int(r.n) if r else 0,
                    "목표 날수": s["backfill_days"] + 1,
                    "가장 오래된 날": r.first if r else "-", "가장 최근 날": r.last if r else "-"})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="나라장터 데이터 수집")
    ap.add_argument("--days", type=int, default=7, help="최근 N일")
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--auto", action="store_true", help="최근 3일 + 과거 데이터 자동 채우기")
    ap.add_argument("--quick", action="store_true", help="어제·오늘 공고/기초금액/개찰결과만 빠르게")
    a = ap.parse_args()
    print("DB:", "원격(PostgreSQL)" if db.is_remote() else db.url())
    if a.auto:
        auto()
        raise SystemExit(0)
    if a.quick:
        quick()
        raise SystemExit(0)
    if a.d1:
        d1 = datetime.strptime(a.d1, "%Y%m%d").date()
        d2 = datetime.strptime(a.d2, "%Y%m%d").date() if a.d2 else date.today()
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=a.days - 1)
    res = collect(d1, d2, progress=lambda t, p: print(f"[{p:5.0%}] {t}", flush=True))
    print(json.dumps({k: {"건수": v["count"], "중단": v["stopped"], "오류": v["errors"][:3]}
                      for k, v in res.items()}, ensure_ascii=False, indent=2))
