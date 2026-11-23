"""국방전자조달(방위사업청 D2B) 시설공사 입찰 수집.

공공데이터포털 '방위사업청_군수품조달정보 입찰공고_GW'(15158416), '…입찰결과_GW'(15158417).
나라장터와 같은 인증키(DATA_GO_KR_KEY)를 쓰며, 포털에서 두 API 를 '활용신청'해야 함.

국방 공고는 나라장터 표(bids·base_amounts·plans·prelim_prices·openings·results·ranks·regions·licenses)에
'D2B…' 공고번호로 함께 저장 → 투찰금액 계산·알림·경쟁사·추천 성적을 그대로 사용.
(G2B 공고번호가 있는 공고는 나라장터에도 올라와 있으므로 건너뜀)

    python d2b.py              # 최근 공고·결과 수집
    python d2b.py --check      # 응답 확인
"""
import json
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests

import api_client
import db
from config import load_settings

BID = "https://apis.data.go.kr/1690000/BidPblancInfoService"
RES = "https://apis.data.go.kr/1690000/BidResultInfoService"
PREFIX = "D2B"
WINDOW = 30               # 목록 조회 기간(일)
NOTICE_DAYS = 45          # 매번 다시 보는 최근 공고 기간
BACKFILL_DAYS = 365       # 결과(개찰) 과거 채우기
# 하루 호출 한도(개발계정 약 1,000회)를 하루 여러 번 실행(새벽 전체 1 + 평일 낮 3)과 사이트 점검이 나눠 쓰도록
BUDGET_FULL = {"notice": 120, "result": 90, "backfill": 150}   # 새벽 전체 수집 (상세 호출 수)
BUDGET_QUICK = {"notice": 40, "result": 30, "backfill": 0}     # 낮 빠른 수집
MAX_DETAIL = 120

A_FIELDS = {"inanAmnt": "국민연금보험료", "inheAmnt": "국민건강보험료", "treaAmnt": "노인장기요양보험료",
            "retiDedu": "퇴직공제부금비", "safeAmnt": "산업안전보건관리비", "safeCost": "안전관리비",
            "quasCost": "품질관리비"}


# ---------------- API ----------------
def _items_from_xml(text: str) -> tuple[list[dict], int]:
    root = ET.fromstring(text)
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "00"
    if code not in ("00", "0", "000"):
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or text[:300]
        api_client._raise(code, msg)
    items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
    total = int(root.findtext(".//totalCount") or len(items) or 0)
    return items, total


def call(base: str, op: str, params: dict, page: int = 1, rows: int = 100) -> tuple[list[dict], int]:
    s = load_settings()
    if not s["service_key"]:
        raise api_client.ApiError("공공데이터포털 인증키가 없습니다.")
    q = {"serviceKey": s["service_key"], "pageNo": page, "numOfRows": rows, "type": "json", "_type": "json"}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    last = None
    for attempt in range(3):
        try:
            r = requests.get(f"{base}/{op}", params=q, timeout=40)
            if r.status_code >= 500:
                raise api_client.ApiError(f"서버 오류 HTTP {r.status_code}")
            text = r.text.strip()
            if text.startswith("<"):
                return _items_from_xml(text)
            return api_client._parse(r)
        except ET.ParseError as e:
            raise api_client.ApiError(f"응답을 읽을 수 없음: {e}")
        except (requests.RequestException, api_client.ApiError) as e:
            last = e
            if isinstance(e, api_client.ApiError) and "서버 오류" not in str(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise api_client.ApiError(f"국방 API 호출 실패: {last}")


def call_all(base: str, op: str, params: dict, rows: int = 100, max_pages: int = 100) -> list[dict]:
    items, total = call(base, op, params, 1, rows)
    out, page = list(items), 2
    while len(out) < total and items and page <= max_pages:
        items, total = call(base, op, params, page, rows)
        out.extend(items)
        page += 1
        time.sleep(0.1)
    return out


def _d(x: date) -> str:
    return x.strftime(load_settings().get("d2b_date_fmt", "%Y%m%d"))


# ---------------- 변환 ----------------
def _num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v
    except (TypeError, ValueError):
        return None


def _dt(x):
    """'20261123110000' / '2026-11-23 11:00' / '202611231100' → 'YYYY-MM-DD HH:MM:SS'"""
    import re
    s = re.sub(r"\D", "", str(x or ""))
    if len(s) >= 12:
        s = (s + "00")[:14]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} 00:00:00"
    return None


def keys(r: dict) -> tuple[str, str, str]:
    """(bid_key, bid_no, bid_ord) — 발주기관코드+공고번호로 나라장터 번호와 겹치지 않게."""
    no = f"{PREFIX}{r.get('orntCode', '')}_{str(r.get('pblancNo', '')).replace('-', '')}"
    ord_ = str(r.get("pblancOdr") or "0").zfill(3)
    return f"{no}-{ord_}", no, ord_


def _split_list(x) -> list[str]:
    import re
    return [p.strip() for p in re.split(r"[,/|;\n]", str(x or "")) if p.strip()]


def on_g2b(no) -> bool:
    """g2bPblancNo 가 실제 나라장터 공고번호로 우리 자료에 있는지.
    (방위사업청은 이 칸에 '년도+기관코드+공고번호+공사번호' 같은 자체 번호를 넣기도 해서 값이 있다고 나라장터 공고는 아님)"""
    no = str(no or "").strip()
    if not no:
        return False
    cands = {no, no.split("-")[0]}
    for c in cands:
        if len(db.query_df("SELECT 1 AS x FROM bids WHERE bid_no = :n", {"n": c})):
            return True
    return False


def is_target(name: str, extra: str = "", keywords=None) -> bool:
    import analysis
    kw = keywords or analysis.company_keywords()
    text = f"{name or ''} {extra or ''}"
    return any(k in text for k in kw)


def map_notice(lst: dict, det: dict) -> dict[str, list[dict]]:
    """목록+상세 → 나라장터 표 형식."""
    r = {**lst, **{k: v for k, v in det.items() if v not in (None, "")}}
    key, no, ord_ = keys(r)
    a_parts = {A_FIELDS[k]: _num(r.get(k)) for k in A_FIELDS if _num(r.get(k))}
    a_val = sum(a_parts.values()) if a_parts else 0.0
    base = _num(r.get("baseAmnt"))
    out = {
        "bids": [{
            "bid_key": key, "bid_no": no, "bid_ord": ord_,
            "name": r.get("cntrwkNm"), "org": r.get("ornt"), "demand_org": r.get("pblictInstt") or r.get("ornt"),
            "region": r.get("lc"), "main_type": r.get("cntrwkSe"),
            "method": r.get("sucbidrDecsnMth"), "contract_method": r.get("cntrctMth"),
            "notice_dt": _dt(r.get("pblancDate")), "close_dt": _dt(r.get("biddocPresentnClosDt")),
            "open_dt": _dt(r.get("opengDt")), "est_price": _num(r.get("estmPrce")),
            "lower_rate": _num(r.get("scsbidLwltRt")), "url": "https://www.d2b.go.kr/",
            "spec_docs": None, "ntce_kind": r.get("pblancSe"),
            "qual_dt": _dt(r.get("bidPartcptReqstClosDt") or r.get("bidPartcptRegistClosDt")), "begin_dt": None,
        }],
        "regions": [{"bid_key": key, "region": x} for x in _split_list(r.get("areaLmttList"))],
        "licenses": [{"bid_key": key, "license": x[:2000]} for x in _split_list(r.get("lcnsLmttList"))],
    }
    if base:
        out["base_amounts"] = [{
            "bid_key": key, "base_amt": base, "range_begin": _num(r.get("asessRtLwlt")),
            "range_end": _num(r.get("asessRtUplmt")), "a_value": a_val, "a_apply": 1 if a_val else 0,
            "pure_cost": None, "a_detail": json.dumps(a_parts, ensure_ascii=False) if a_parts else None,
        }]
    return out


def map_result(lst: dict, det: dict, prelim: list[dict], bidders: list[dict]) -> dict[str, list[dict]]:
    r = {**lst, **{k: v for k, v in det.items() if v not in (None, "")}}
    key, no, ord_ = keys(r)
    open_dt = _dt(r.get("opengDt") or r.get("opengDate"))
    base = _num(r.get("bsisPreparPc"))
    out: dict[str, list[dict]] = {}
    # 복수예비가격: 선택된 것들의 평균 = 예정가격
    pp = [(int(_num(p.get("epprRank")) or 0), _num(p.get("planPrce")), str(p.get("choiYsno", "")).upper() == "Y")
          for p in prelim if _num(p.get("planPrce"))]
    if pp:
        out["prelim_prices"] = [{"bid_key": key, "sno": s, "price": pr, "drawn": int(c), "draw_count": None}
                                for s, pr, c in pp]
        chosen = [pr for _, pr, c in pp if c]
        if chosen:
            out["plans"] = [{"bid_key": key, "plan_price": round(sum(chosen) / len(chosen)), "base_amt": base,
                             "open_dt": open_dt}]
    # 참가업체·순위
    rows = []
    for b in bidders:
        rk = _num(b.get("bidnRank"))
        amt = _num(b.get("tbidAmnt"))
        if amt:
            rows.append({"rank": int(rk) if rk else 0, "bizno": str(b.get("bznsRgnb") or "").replace("-", ""),
                         "company": b.get("mfkrName"), "amount": amt, "rate": _num(b.get("bidnRate")),
                         "note": b.get("bidxNote") or b.get("ubidCaus")})
    ranked = sorted([x for x in rows if x["rank"] > 0], key=lambda x: x["rank"])
    if rows:
        first = ranked[0] if ranked else None
        out["openings"] = [{"bid_key": key, "open_dt": open_dt, "participants": len(rows),
                            "status": r.get("bidResult"), "first_company": first and first["company"],
                            "first_bizno": first and first["bizno"], "first_amt": first and first["amount"],
                            "first_rate": first and first["rate"]}]
        out["ranks"] = [{"bid_key": key, **x} for x in ranked[:100]]
    win_biz = str(r.get("scsbidEntrpsBsnmRegistNo") or "").replace("-", "")
    if win_biz or r.get("scsbidEntrpsNm"):
        w = next((x for x in rows if x["bizno"] and x["bizno"] == win_biz), None)
        out["results"] = [{"bid_key": key, "open_dt": open_dt, "participants": len(rows) or None,
                           "winner": r.get("scsbidEntrpsNm"), "winner_bizno": win_biz or None,
                           "win_amt": w and w["amount"], "win_rate": w and w["rate"]}]
    if base:
        out["base_amounts"] = [{"bid_key": key, "base_amt": base, "range_begin": _num(r.get("asessRtLwlt")),
                                "range_end": _num(r.get("asessRtUplmt"))}]
    return out


# ---------------- 수집 ----------------
def _done(op: str) -> set:
    try:
        return set(db.query_df("SELECT bid_no FROM limit_checks WHERE op = :o", {"o": op})["bid_no"])
    except Exception:  # noqa: BLE001
        return set()


def _mark(op: str, key: str, n: int) -> None:
    db.upsert_many("limit_checks", [{"bid_no": key, "op": op, "n": n, "checked_at": db.now()}])


def _save(tables: dict[str, list[dict]], skip_existing_bid: bool = False) -> None:
    for t, rows in tables.items():
        if not rows:
            continue
        if t == "bids" and skip_existing_bid:
            have = set(db.query_df("SELECT bid_key FROM bids WHERE bid_key = :k", {"k": rows[0]["bid_key"]})["bid_key"])
            rows = [x for x in rows if x["bid_key"] not in have]
        if t == "base_amounts" and skip_existing_bid:
            # 결과 쪽 기초금액은 공고 쪽 A값을 지우지 않도록 비어 있을 때만
            have = db.query_df("SELECT bid_key FROM base_amounts WHERE bid_key = :k", {"k": rows[0]["bid_key"]})
            if len(have):
                continue
        db.upsert_many(t, rows)


def collect_notices(d1: date, d2: date, progress=None, budget: int = MAX_DETAIL) -> tuple[int, int]:
    """시설 경쟁입찰공고 목록 → (G2B 번호 없는) 공사 공고 상세 → 저장. (목록 건수, 저장 건수)"""
    lst = []
    d = d1
    while d <= d2:
        e = min(d + timedelta(days=WINDOW - 1), d2)
        lst += call_all(BID, "getFcltyCmpetBidPblancList", {"anmtDateBegin": _d(d), "anmtDateEnd": _d(e)})
        d = e + timedelta(days=1)
    done = _done("d2b_notice")
    saved = 0
    # 개찰이 아직 안 된 공고부터 (참가등록·투찰을 놓치지 않는 게 먼저)
    lst.sort(key=lambda x: str(x.get("opengDt") or ""), reverse=True)
    for r in lst:
        if str(r.get("busiDivs", "공사")).find("공사") < 0 or "용역" in str(r.get("cntrwkNm", "")):
            continue
        if on_g2b(r.get("g2bPblancNo")):
            continue   # 나라장터에도 올라온 공고 → 나라장터 쪽 자료 사용
        key = keys(r)[0]
        if key in done:
            continue
        if not is_target(r.get("cntrwkNm"), "") and saved >= budget // 2:
            continue   # 이름에 업종 키워드가 없으면 상세(면허) 확인은 여유 있을 때만
        if budget <= 0:
            break
        params = {k: r.get(k) for k in ("pblancYear", "pblancSeCode", "pblancNo", "pblancOdr", "cntrwkNo", "orntCode")}
        try:
            det = (call(BID, "getFcltyCmpetBidPblancDetail", params)[0] or [{}])[0]
        except api_client.QuotaError:
            raise
        except api_client.ApiError:
            continue
        budget -= 1
        tb = map_notice(r, det)
        lic = " ".join(x["license"] for x in tb["licenses"])
        if is_target(tb["bids"][0]["name"], f"{tb['bids'][0]['main_type'] or ''} {lic}"):
            _save(tb)
            saved += 1
        _mark("d2b_notice", key, 1)
    if progress:
        progress(f"국방 공고: 목록 {len(lst):,}건 · 우리 업종 저장 {saved}건")
    return len(lst), saved


def collect_results(d1: date, d2: date, progress=None, budget: int = MAX_DETAIL) -> tuple[int, int, bool]:
    """시설 경쟁입찰결과 목록 → 우리 업종 공고의 상세·복수예비가격·참가업체 → 저장."""
    lst = []
    d = d1
    while d <= d2:
        e = min(d + timedelta(days=WINDOW - 1), d2)
        lst += call_all(RES, "getFcltyCmpetBidResultList", {"opengDateBegin": _d(d), "opengDateEnd": _d(e)})
        d = e + timedelta(days=1)
    done = _done("d2b_result")
    known = set(db.query_df("SELECT bid_key FROM bids WHERE bid_key LIKE 'D2B%'")["bid_key"])
    saved, complete = 0, True
    for r in sorted(lst, key=lambda x: str(x.get("opengDt") or x.get("opengDate") or ""), reverse=True):
        key = keys(r)[0]
        if key in done or "용역" in str(r.get("cntrwkNm", "")) or not (key in known or is_target(r.get("cntrwkNm"))):
            continue
        if budget <= 0:
            complete = False
            break
        od = str(r.get("opengDate") or r.get("opengDt") or "")[:10].replace("-", "")[:8]
        base_p = {"cntrwkNo": r.get("cntrwkNo"), "orntCode": r.get("orntCode")}
        try:
            det = (call(RES, "getFcltyCmpetBidResultDetail",
                        {**base_p, "opengDate": od, "pblancNo": r.get("pblancNo"), "pblancOdr": r.get("pblancOdr")})[0]
                   or [{}])[0]
            prelim = call_all(RES, "getFcltyCmpetBidResultBsicList", {**base_p, "ntatPlanDate": od})
            bidders = call_all(RES, "getFcltyCmpetBidResultMnufList", {**base_p, "ntatPlanDate": od})
        except api_client.QuotaError:
            raise
        except api_client.ApiError:
            continue
        budget -= 3
        tb = map_result(r, det, prelim, bidders)
        if key not in known:
            # 공고 정보를 못 받은 공고: 결과 목록·상세의 정보로 최소한의 공고 행 만들기
            tb["bids"] = [{"bid_key": key, "bid_no": keys(r)[1], "bid_ord": keys(r)[2], "name": r.get("cntrwkNm"),
                           "org": r.get("ornt"), "demand_org": r.get("ornt"), "region": None,
                           "main_type": det.get("cntrwkSe"), "method": r.get("sucbidrDecsnMth"),
                           "contract_method": r.get("cntrctMth"), "notice_dt": None, "close_dt": None,
                           "open_dt": _dt(r.get("opengDt") or r.get("opengDate")), "est_price": None,
                           "lower_rate": _num(det.get("scsbidLwltRt")), "url": "https://www.d2b.go.kr/",
                           "spec_docs": None, "ntce_kind": r.get("pblancSe"), "qual_dt": None, "begin_dt": None}]
        _save(tb, skip_existing_bid=True)
        if key in known and _num(det.get("scsbidLwltRt")):
            db.execute("UPDATE bids SET lower_rate = :r WHERE bid_key = :k AND lower_rate IS NULL",
                       {"r": _num(det.get("scsbidLwltRt")), "k": key})
        _mark("d2b_result", key, len(bidders))
        _record_ours(key, bidders)
        saved += 1
    if progress:
        progress(f"국방 개찰결과: 목록 {len(lst):,}건 · 우리 업종 저장 {saved}건" + ("" if complete else " (다음에 이어서)"))
    return len(lst), saved, complete


def _record_ours(key: str, bidders: list[dict]) -> None:
    """참가업체 목록에 우리 회사가 있으면 '우리 투찰'에 실제 금액·순위로 기록."""
    biz = str(db.company()["bizno"] or "").replace("-", "")
    if not biz:
        return
    ours = next((b for b in bidders if str(b.get("bznsRgnb") or "").replace("-", "") == biz), None)
    if not ours or not _num(ours.get("tbidAmnt")):
        return
    old = db.query_df("SELECT amount, memo, created_at FROM my_bids WHERE bid_key = :k", {"k": key})
    db.upsert_many("my_bids", [{
        "bid_key": key, "amount": _num(ours.get("tbidAmnt")), "sajung": None,
        "memo": old["memo"].iloc[0] if len(old) else "국방 참가업체에서 자동 발견",
        "my_rank": int(_num(ours.get("bidnRank")) or 0), "checked_at": db.now(),
        "created_at": old["created_at"].iloc[0] if len(old) else db.now(),
        "rec_amount": old["amount"].iloc[0] if len(old) else None, "amount_source": "개찰순위(실제)"}])


def auto(progress=print, quick: bool = False) -> str:
    """최근 공고(먼저) + 최근 결과 (+ 결과 과거 채우기). 하루 호출 한도를 나눠 쓰고, 넘으면 다음에 이어서."""
    today = date.today()
    b = BUDGET_QUICK if quick else BUDGET_FULL
    msgs = []
    try:
        n1, s1 = collect_notices(today - timedelta(days=3 if quick else NOTICE_DAYS), today, progress,
                                 budget=b["notice"])
        msgs.append(f"공고 {s1}건")
        _, s2, _ = collect_results(today - timedelta(days=3 if quick else 30), today, progress, budget=b["result"])
        msgs.append(f"결과 {s2}건")
        if b["backfill"]:
            back = db.get_kv("d2b_backfill_to")
            start = pd.Timestamp(back).date() if back else today - timedelta(days=31)
            oldest = today - timedelta(days=BACKFILL_DAYS)
            if start > oldest:
                b1 = max(oldest, start - timedelta(days=30))
                _, s3, complete = collect_results(b1, start, progress, budget=b["backfill"])
                if complete:   # 이 기간을 다 끝냈을 때만 다음 기간으로 (못 끝낸 공고를 건너뛰지 않게)
                    db.set_kv("d2b_backfill_to", (b1 - timedelta(days=1)).isoformat())
                msgs.append(f"과거 결과 {s3}건 ({b1}~{start}{'' if complete else ', 이어서'})")
    except api_client.QuotaError as e:
        msgs.append(f"호출 한도 초과 → 다음에 이어서 ({e})")
    except api_client.KeyError_ as e:
        msgs.append(f"인증 오류: 공공데이터포털에서 방위사업청 입찰공고·입찰결과 API 활용신청 필요 ({e})")
    msg = "국방: " + " · ".join(msgs)
    db.log("국방 입찰(자동)", f"~{today}", 0, msg)
    return msg


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        t = date.today()
        for base, op, p in ((BID, "getFcltyCmpetBidPblancList", {"anmtDateBegin": _d(t - timedelta(days=7)), "anmtDateEnd": _d(t)}),
                            (RES, "getFcltyCmpetBidResultList", {"opengDateBegin": _d(t - timedelta(days=7)), "opengDateEnd": _d(t)})):
            items, total = call(base, op, p, rows=3)
            print(op, total, json.dumps(items[:1], ensure_ascii=False)[:1500])
    else:
        print(auto())
