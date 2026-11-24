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
import pandas as pd
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
        "spec_docs": _spec_docs(r),
        "ntce_kind": g(r, "ntceKindNm"),      # 등록공고 / 변경공고 / 취소공고 / 재공고 …
        "qual_dt": g(r, "bidQlfctRgstDt"),    # 입찰참가자격 등록(참가신청) 마감
        "begin_dt": g(r, "bidBeginDt"),       # 투찰 시작
        # 참가 자격 판단용 (실적·PQ·지명·시공능력 제한이 있으면 우리가 못 들어갈 수 있음)
        "perf_limit": g(r, "arsltCmptYn"),                     # 실적경쟁 여부 Y/N
        "pq": g(r, "pqEvalYn"),                                # PQ 심사 여부
        "designated": g(r, "dsgntCmptYn"),                     # 지명경쟁 여부
        "ability_list": (str(g(r, "cnstrtnAbltyEvlAmtList", default="") or "")[:500] or None),  # 시공능력평가금액 제한
        "joint_duty": g(r, "rgnDutyJntcontrctYn"),             # 지역의무공동도급 여부
    })]


def _spec_docs(r: dict) -> str | None:
    """공고문 첨부파일 [(파일명, 주소), ...] (JSON)."""
    docs = []
    for i in range(1, 11):
        u, n = r.get(f"ntceSpecDocUrl{i}"), r.get(f"ntceSpecFileNm{i}")
        if u:
            docs.append([n or f"첨부{i}", u])
    return json.dumps(docs, ensure_ascii=False) if docs else None


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
    grp = g(r, "lmtGrpNo", default="")
    if text and grp:
        text = f"[G{grp}] {text}"   # 면허 묶음 번호 (같은 묶음 = 모두 필요)
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
    if op != "bid_list":
        # 공고(bids)가 없는 자료는 버림: 다른 업종이라 지운 공고의 개찰결과·예비가격이 다시 쌓이는 것을 막음
        known = _known_bid_nos()
        for table in list(batches):
            batches[table] = [r for r in batches[table] if r["bid_key"].split("-")[0] in known]
    for table, rows in batches.items():
        if table == "bids":
            _KNOWN["nos"] = None  # 새 공고가 들어왔으니 다음에 다시 읽음
        db.upsert_many(table, rows)
    # 평일인데 0건이면 (호출 한도·일시 장애 가능성) '완료'로 표시하지 않고 다음에 다시 받음
    if items or d.weekday() >= 5 or op not in MUST_HAVE_DATA:
        db.mark_day(op, d.isoformat(), len(items))
    return len(items)


_KNOWN = {"nos": None}


def _known_bid_nos() -> set[str]:
    if _KNOWN["nos"] is None:
        _KNOWN["nos"] = db.known_bid_nos()
    return _KNOWN["nos"]


# 평일이면 반드시 자료가 있어야 하는 항목
MUST_HAVE_DATA = {"bid_list", "base_amount", "openings", "prelim_prices", "winners"}


def repair_empty_days() -> int:
    """예전에 평일 0건으로 '완료' 표시된 날을 지워서 다시 받게 함."""
    cd = db.query_df("SELECT op, day FROM collect_days WHERE rows = 0")
    if cd.empty:
        return 0
    cd = cd[cd["op"].isin(MUST_HAVE_DATA) & (pd.to_datetime(cd["day"], errors="coerce").dt.weekday < 5)]
    for r in cd.itertuples():
        db.execute("DELETE FROM collect_days WHERE op = :o AND day = :d", {"o": r.op, "d": r.day})
    return len(cd)


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


def _cleanup(progress=None) -> None:
    try:
        import analysis
        kw = analysis.company_keywords()
        n = db.prune_prelim(kw) + db.prune_nontarget(kw) + db.prune_orphans()
        _KNOWN["nos"] = None
        db.vacuum()
        if progress:
            progress(f"사전 정리 {n:,}행 · DB {db.size_mb() or 0:,.0f}MB")
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"사전 정리 실패: {e}")


def auto(progress=print) -> dict:
    """매일 자동 수집: 최근 3일은 항상 새로 받고, 아직 안 받은 과거 날짜를 최신순으로 채움."""
    s = load_settings()
    today = date.today()
    recent = [today - timedelta(days=i) for i in range(2, -1, -1)]
    oldest = today - timedelta(days=s["backfill_days"])
    summary = {}
    backfilled = False
    _cleanup(progress)  # 채우기 전에 먼저 공간 확보 (무료 한도 500MB)
    if db.get_kv("bidlist_fields_v2") != "done":
        # 새로 추가한 참가자격 항목(실적·PQ·지명·시공능력)을 채우려고 최근 45일 공고 목록을 다시 받음 (한 번만)
        for i in range(45):
            db.execute("DELETE FROM collect_days WHERE op = 'bid_list' AND day = :d",
                       {"d": (date.today() - timedelta(days=i)).isoformat()})
        db.set_kv("bidlist_fields_v2", "done")
    fixed = repair_empty_days()
    if fixed and progress:
        progress(f"0건으로 끝났던 평일 {fixed}일을 다시 받기로 표시")
    for op, _, label in JOBS:
        done = db.done_days(op)
        missing = []
        d = today - timedelta(days=3)
        while d >= oldest and len(missing) < s["backfill_per_run"]:
            if d.isoformat() not in done:
                missing.append(d)
            d -= timedelta(days=1)
        days = recent + missing
        # 최근 2주 안의 날짜는 분석 표가 어차피 새로 받으므로, 그보다 오래된 날짜에서 자료가 나왔을 때만
        # '과거 자료를 채웠다'로 봄 (매번 전체를 다시 내려받아 전송량을 낭비하지 않게)
        near_cut = today - timedelta(days=14)
        old = [x for x in missing if x < near_cut]
        res = collect_days(op, recent + [x for x in missing if x >= near_cut])
        if old and not res["stopped"]:
            r2 = collect_days(op, old)
            if r2["count"]:
                backfilled = True
            res = {"count": res["count"] + r2["count"], "days": len(days), "errors": res["errors"] + r2["errors"],
                   "stopped": r2["stopped"]}
        msg = f"최근 3일 + 과거 {len(missing)}일"
        if res["stopped"]:
            msg += " / " + res["stopped"]
        if res["errors"]:
            msg += " / 오류: " + "; ".join(res["errors"][:3])
        db.log(label + "(자동)", f"{days[-1] if missing else recent[0]}~{today}", res["count"], msg)
        summary[label] = res
        if progress:
            progress(f"{label}: {res['count']:,}건, {msg}  [누적 내려받음 {db.read_mb():,.0f}MB]")
    try:
        import d2b
        summary["국방"] = d2b.auto(progress)
        # 국방 과거 결과는 분석 표가 국방 공고를 매번 새로 받으므로 전체 다시 받기 표시를 하지 않음
        if progress:
            progress(summary["국방"])
    except Exception as e:  # noqa: BLE001  국방 문제로 나라장터 수집이 멈추지 않게
        if progress:
            progress(f"국방 수집 실패: {e}")
    try:
        import kepco
        summary["한전"] = kepco.auto(progress)
        if progress:
            progress(summary["한전"])
    except Exception as e:  # noqa: BLE001  한전 문제로 나라장터 수집이 멈추지 않게
        if progress:
            progress(f"한전 수집 실패: {e}")
    try:
        summary["지역·면허 재확인"] = verify_limits(progress)
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"지역·면허 재확인 실패: {e}")
    if backfilled:
        # 과거 자료를 새로 채웠으면 분석 표 캐시를 한 번 전체 다시 받게 표시
        db.set_kv("dataset_full_reload_after", pd.Timestamp.now().isoformat())
    if progress:
        progress(f"[누적 내려받음 {db.read_mb():,.0f}MB] 국방·한전·지역면허 확인 후")
    r = collect_ranks_auto()
    summary["개찰순위"] = r
    if progress:
        progress(f"[누적 내려받음 {db.read_mb():,.0f}MB] 개찰순위 후")
    summary["우리 투찰"] = update_my_bids(progress)
    if progress:
        progress(f"[누적 내려받음 {db.read_mb():,.0f}MB] 우리 투찰 확인 후")
    if progress:
        progress(f"개찰순위: 공고 {r['bids']}건 / 업체 {r['rows']:,}행 {r['stopped']}")
    summary["저장 공간"] = maintenance(progress)
    # 매주 월요일(또는 7일 넘게 안 했으면) 추천 방식 자동 검증
    try:
        import tune
        if tune.due():
            summary["추천 검증"] = tune.run(progress)
    except Exception as e:  # noqa: BLE001  (검증 실패가 수집 전체를 실패로 만들지 않게)
        db.log("추천 검증", "-", 0, f"오류: {e}")
    try:
        import notify
        summary["주간 리포트"] = notify.send_weekly_if_due()
    except Exception as e:  # noqa: BLE001
        db.log("주간 리포트", "-", 0, f"오류: {e}")
    return summary


def maintenance(progress=None) -> str:
    """저장 공간 정리·점검. 한도의 80%를 넘으면 알림."""
    import analysis
    s = load_settings()
    kw = analysis.company_keywords()
    removed = db.prune_prelim(kw)
    other = db.prune_nontarget(kw) + db.prune_orphans()
    _KNOWN["nos"] = None
    try:
        db.vacuum()
        print("압축:", ", ".join(db.compact(s["db_limit_mb"])))
    except Exception as e:  # noqa: BLE001
        print("VACUUM 실패:", e)
    mb = db.size_mb()
    msg = f"예비가격 상세 정리 {removed:,}행 · 다른 업종 지역·면허 정리 {other:,}행" + (f" · DB {mb:,.0f}MB / {s['db_limit_mb']}MB" if mb else "")
    db.log("저장 공간", "-", removed, msg)
    if mb and mb > s["db_limit_mb"] * 0.8:
        try:
            import notify
            notify.send("🚨 입찰 분석기 저장 공간 경고",
                        f"데이터베이스 용량이 {mb:,.0f}MB 로 한도({s['db_limit_mb']}MB)의 80%를 넘었습니다.\n"
                        "Claude 에게 '저장 공간 정리'를 요청해 주세요.")
        except Exception as e:  # noqa: BLE001
            msg += f" (경고 전송 실패: {e})"
    if progress:
        progress(msg)
    return msg


def verify_limits(progress=None, max_bids: int = 150) -> str:
    """개찰 전·우리 업종 공고 중 참가지역/면허제한 정보가 없는 공고를 공고번호로 직접 조회.

    날짜로 받은 자료에 빠진 공고(정정공고·등록일 차이 등)를 '제한없음'으로 잘못 보는 것을 막음.
    한 공고는 한 번만 확인 (limit_checks 에 기록)."""
    import analysis
    s = load_settings()
    df = analysis.load_dataset()
    if df.empty:
        return "공고 없음"
    now = pd.Timestamp.now()
    t = analysis.apply_filters(df[df["open_date"] >= now], keywords=analysis.company_keywords())
    checked = db.query_df("SELECT bid_no, op FROM limit_checks")
    done = set(zip(checked["bid_no"], checked["op"]))
    n_bid, n_rows, stopped, errs, last_err = 0, 0, "", 0, ""
    for op, col in (("regions", "allowed_regions"), ("licenses", "licenses")):
        need = t[(t[col] == "") & ~t["bid_no"].map(lambda b: (b, op) in done).astype(bool)]
        for bid_no, bid_ord in need[["bid_no", "bid_ord"]].head(max_bids).itertuples(index=False):
            try:
                items = []
                # 차수(bidNtceOrd)가 필수: 최신 차수로 보고, 없으면 원공고(000)도 확인
                for o in dict.fromkeys([str(bid_ord or "000"), "000"]):
                    params = {"inqryDiv": s["inqry_div_bidno"].get(op, "2"), "bidNtceNo": bid_no, "bidNtceOrd": o}
                    items = api_client.call_all(op, params)
                    if items:
                        break
            except api_client.QuotaError as e:
                stopped = str(e)
                break
            except api_client.ApiError as e:
                errs += 1
                last_err = str(e)[:120]
                if errs >= 5 and n_bid == 0:   # 공고번호 조회를 지원하지 않는 경우 헛호출 방지
                    stopped = f"{op} 공고번호 조회 실패: {last_err}"
                    break
                continue
            mapper = map_region if op == "regions" else map_license
            rows = [row for r in items for _, row in mapper(r) if row.get("bid_key")]
            db.upsert_many(op, rows)
            db.upsert_many("limit_checks", [{"bid_no": bid_no, "op": op, "n": len(rows), "checked_at": db.now()}])
            n_bid += 1
            n_rows += len(rows)
        if stopped and "한도" in stopped:
            break
    # 면허 묶음 번호(같은 묶음 = 모두 필요)가 없는 예전 자료: 개찰 전 공고만 공고번호로 다시 받아 묶음까지 저장
    n_grp = 0
    if not stopped:
        has_lic = t[(t["licenses"] != "") & ~t["bid_no"].map(lambda b: (b, "licenses_grp") in done).astype(bool)]
        # 지금 '참가 가능'으로 보이는 공고, 마감이 가까운 공고부터
        has_lic = has_lic.assign(_el=analysis.eligible(has_lic)).sort_values(["_el", "open_date"], ascending=[False, True])
        for bid_no, bid_ord in has_lic[["bid_no", "bid_ord"]].head(max_bids * 3).itertuples(index=False):
            try:
                items = []
                for o in dict.fromkeys([str(bid_ord or "000"), "000"]):
                    items = api_client.call_all("licenses", {"inqryDiv": s["inqry_div_bidno"].get("licenses", "2"),
                                                             "bidNtceNo": bid_no, "bidNtceOrd": o})
                    if items:
                        break
            except api_client.QuotaError as e:
                stopped = str(e)
                break
            except api_client.ApiError:
                continue
            rows = [row for r in items for _, row in map_license(r) if row.get("bid_key")]
            if rows:
                db.execute("DELETE FROM licenses WHERE bid_key LIKE :k", {"k": f"{bid_no}-%"})
                db.upsert_many("licenses", rows)
            db.upsert_many("limit_checks", [{"bid_no": bid_no, "op": "licenses_grp", "n": len(rows), "checked_at": db.now()}])
            n_grp += 1
    msg = f"공고번호로 지역·면허 재확인 {n_bid}건 (찾은 제한 {n_rows}건) · 면허 묶음 다시 받기 {n_grp}건 {stopped}"
    db.log("지역·면허 재확인", "-", n_rows, msg)
    if progress:
        progress(msg)
    return msg


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
    try:
        summary["지역·면허 재확인"] = verify_limits(progress)
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"지역·면허 재확인 실패: {e}")
    try:
        import d2b
        summary["국방"] = d2b.auto(progress, quick=True)
        if progress:
            progress(summary["국방"])
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"국방 수집 실패: {e}")
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


def _biz(x) -> str:
    return str(x or "").replace("-", "").strip()


def find_our_bid(bid_no: str, bid_ord: str, bizno: str) -> dict | None:
    """개찰순위 전체에서 우리 회사 줄을 찾음. 상위 100개 + 우리 줄만 저장 (저장 공간 절약)."""
    s = load_settings()
    params = {"bidNtceNo": bid_no, "bidNtceOrd": bid_ord}
    div = s["inqry_div_bidno"].get("ranks")
    if div:
        params["inqryDiv"] = div
    items = api_client.call_all("ranks", params)
    key = f"{bid_no}-{bid_ord}"
    rows = [map_rank(key, r) for r in items]
    ours = next((r for r in rows if _biz(r["bizno"]) == bizno), None)
    keep = sorted((r for r in rows if r["rank"] > 0), key=lambda r: r["rank"])[:100]
    if ours and ours not in keep:
        keep.append(ours)
    db.upsert_many("ranks", keep or [{"bid_key": key, "rank": 0, "bizno": "", "company": None,
                                      "amount": None, "rate": None, "note": "순위없음"}])
    return ours


def update_my_bids(progress=None, scan_limit: int = 200) -> str:
    """우리 회사 투찰 결과 채우기 (실제 투찰금액·순위는 개찰순위에서 사업자번호로 찾음).

    1) 기록한 공고가 개찰되면 개찰순위 전체에서 우리 줄을 찾아 '실제 금액'과 순위를 채움
    2) 기록하지 않았어도, 최근 개찰된 우리 참가 가능 공고는 개찰순위를 훑어 우리가 넣은 공고를 자동 추가
    """
    c = db.company()
    biz = _biz(c["bizno"])
    if not biz:
        return "회사 사업자번호 미등록 → 건너뜀"
    found, checked, scanned, stopped = 0, 0, 0, ""
    mb = db.query_df("SELECT m.bid_key, m.amount, m.rec_amount FROM my_bids m JOIN openings o ON o.bid_key = m.bid_key "
                     "WHERE m.my_rank IS NULL AND o.participants > 0")
    for r in mb.itertuples():
        if str(r.bid_key).startswith("D2B"):
            continue
        no, ord_ = r.bid_key.rsplit("-", 1)
        try:
            ours = find_our_bid(no, ord_, biz)
        except api_client.QuotaError as e:
            stopped = str(e)
            break
        except api_client.ApiError:
            continue
        upd = {"r": int(ours["rank"]) if ours else 0, "t": db.now(), "k": r.bid_key}
        if ours and ours["amount"]:
            db.execute("UPDATE my_bids SET my_rank = :r, checked_at = :t, amount = :a, sajung = NULL, "
                       "amount_source = '개찰순위(실제)', "
                       "rec_amount = COALESCE(rec_amount, :old) WHERE bid_key = :k",
                       {**upd, "a": ours["amount"], "old": r.amount})
        else:
            db.execute("UPDATE my_bids SET my_rank = :r, checked_at = :t WHERE bid_key = :k", upd)
        checked += 1
        found += bool(ours)
    # 기록하지 않은 공고도 찾기: 최근 4일 개찰된 우리 업종·참가 가능 공고
    if not stopped:
        try:
            import analysis
            df = analysis.load_dataset()
            now = pd.Timestamp.now()
            t = analysis.apply_filters(df[(df["open_date"] < now) & (df["open_date"] >= now - pd.Timedelta(days=4))
                                          & (df["participants"] > 0)],
                                       keywords=analysis.company_keywords(), eligible_only=True)
            t = t[~t["bid_key"].astype(str).str.startswith("D2B")]
            have = set(db.query_df("SELECT bid_key FROM my_bids")["bid_key"])
            done = set(db.query_df("SELECT bid_no FROM limit_checks WHERE op = 'mine_scan'")["bid_no"])
            for r in t[~t["bid_key"].isin(have) & ~t["bid_key"].isin(done)].head(scan_limit).itertuples():
                try:
                    ours = find_our_bid(r.bid_no, r.bid_ord, biz)
                except api_client.QuotaError as e:
                    stopped = str(e)
                    break
                except api_client.ApiError:
                    continue
                db.upsert_many("limit_checks", [{"bid_no": r.bid_key, "op": "mine_scan", "n": int(bool(ours)),
                                                 "checked_at": db.now()}])
                scanned += 1
                if ours:
                    db.upsert_many("my_bids", [{"bid_key": r.bid_key, "amount": ours["amount"], "sajung": None,
                                                "memo": "개찰순위에서 자동 발견", "my_rank": int(ours["rank"]),
                                                "checked_at": db.now(), "created_at": db.now(),
                                                "rec_amount": None, "amount_source": "개찰순위(실제)"}])
                    found += 1
        except Exception as e:  # noqa: BLE001
            stopped = stopped or f"자동 찾기 실패: {e}"
    # 이미 받은 개찰순위(상위권)에 우리 회사가 있는데 기록이 없는 공고
    rk = db.query_df("SELECT bid_key, rank, amount, bizno FROM ranks WHERE rank > 0 AND bizno IS NOT NULL AND bizno <> ''")
    rk = rk[rk["bizno"].map(_biz) == biz]
    have = set(db.query_df("SELECT bid_key FROM my_bids")["bid_key"])
    auto = rk[~rk["bid_key"].isin(have)]
    if len(auto):
        db.upsert_many("my_bids", [{"bid_key": r.bid_key, "amount": r.amount, "sajung": None,
                                    "memo": "개찰순위에서 자동 발견", "my_rank": int(r.rank),
                                    "checked_at": db.now(), "created_at": db.now(),
                                    "rec_amount": None, "amount_source": "개찰순위(실제)"}
                                   for r in auto.itertuples()])
    msg = (f"결과 확인 {checked}건(순위 찾음 {found}) · 개찰순위 훑기 {scanned}건 · 자동 발견 {len(auto)}건 "
           f"{stopped}")
    db.log("우리 투찰", "-", checked + len(auto), msg)
    if progress:
        progress(msg)
    return msg


def refresh_my_results(max_bids: int = 10, days: int = 14) -> str:
    """사이트에서 '우리 투찰'을 열 때: 개찰 시간이 지났는데 결과가 아직 없는 우리 투찰을 공고번호로 바로 조회.

    개찰순위(업체별 금액) → 참가업체수·1순위·우리 순위와 실제 금액, 예비가격 상세 → 예정가격(실제 사정율).
    자동 수집(하루 몇 번)을 기다리지 않고 개찰 직후 바로 결과를 볼 수 있게 함."""
    c = db.company()
    biz = _biz(c["bizno"])
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    nows = datetime.now().strftime("%Y-%m-%d %H:%M")
    q = db.query_df("""SELECT m.bid_key, m.amount, m.my_rank, b.open_dt, p.plan_price, o.participants
                       FROM my_bids m JOIN bids b ON b.bid_key = m.bid_key
                       LEFT JOIN plans p ON p.bid_key = m.bid_key LEFT JOIN openings o ON o.bid_key = m.bid_key
                       WHERE b.open_dt IS NOT NULL AND b.open_dt <= :now AND b.open_dt >= :since""",
                    {"now": nows, "since": since})
    if q.empty:
        return "확인할 공고 없음"
    q = q[~q["bid_key"].astype(str).str.startswith(("D2B", "KEPCO"))]
    need = q[q["my_rank"].isna() | q["plan_price"].isna() | q["participants"].isna()].head(max_bids)
    n_done, n_found, err = 0, 0, ""
    for r in need.itertuples():
        no, ord_ = r.bid_key.rsplit("-", 1)
        try:
            items = api_client.call_all("ranks", {"bidNtceNo": no, "bidNtceOrd": ord_})
            rows = [map_rank(r.bid_key, x) for x in items]
            ranked = sorted((x for x in rows if x["rank"] > 0), key=lambda x: x["rank"])
            if ranked:
                ours = next((x for x in rows if biz and _biz(x["bizno"]) == biz), None)
                keep = ranked[:100] + ([ours] if ours and ours not in ranked[:100] else [])
                db.upsert_many("ranks", keep)
                if pd.isna(r.participants):
                    f = ranked[0]
                    db.upsert_many("openings", [{"bid_key": r.bid_key, "open_dt": r.open_dt, "participants": len(rows),
                                                 "status": "개찰완료", "first_company": f["company"],
                                                 "first_bizno": f["bizno"], "first_amt": f["amount"],
                                                 "first_rate": f["rate"]}])
                if ours:
                    n_found += 1
                    db.execute("UPDATE my_bids SET my_rank = :r, checked_at = :t, amount = :a, sajung = NULL, "
                               "amount_source = '개찰순위(실제)', rec_amount = COALESCE(rec_amount, :old) "
                               "WHERE bid_key = :k", {"r": int(ours["rank"]), "t": db.now(), "a": ours["amount"],
                                                       "old": r.amount, "k": r.bid_key})
                elif biz:
                    db.execute("UPDATE my_bids SET my_rank = 0, checked_at = :t WHERE bid_key = :k",
                               {"t": db.now(), "k": r.bid_key})
            if pd.isna(r.plan_price):
                pre = api_client.call_all("prelim_prices", {"inqryDiv": load_settings()["inqry_div_bidno"].get(
                    "prelim_prices", "2"), "bidNtceNo": no, "bidNtceOrd": ord_})
                batches: dict[str, list] = {}
                for x in pre:
                    for table, row in map_prelim(x):
                        if row.get("bid_key"):
                            batches.setdefault(table, []).append(row)
                for table, rows_ in batches.items():
                    db.upsert_many(table, rows_)
            n_done += 1
        except api_client.QuotaError as e:
            err = f" (호출 한도: {e})"
            break
        except api_client.ApiError as e:
            err = f" (오류: {str(e)[:80]})"
            continue
    return f"개찰 확인 {n_done}건 · 우리 순위 찾음 {n_found}건{err}"


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


def print_status() -> None:
    """실행 기록(로그)에 남기는 점검 요약 — 건수·비율만, 회사 정보는 넣지 않음."""
    try:
        import analysis
        df = analysis.load_dataset()
        print("=== 점검 요약 ===")
        print(f"DB 용량: {db.size_mb() or 0:,.0f}MB")
        for r in backfill_status():
            print(f"채우기 {r['항목']}: {r['수집된 날수']}/{r['목표 날수']}일")
        h = analysis.data_health(df)
        if len(h):
            print(h.to_string(index=False))
        ts = db.table_sizes()
        if len(ts):
            print("테이블 용량:", ", ".join(f"{r['테이블']} {r['MB'] if r['MB'] is not None else '-'}MB/{int(r['행 수']):,}행"
                                          for _, r in ts.head(8).iterrows()))
        print("추천 방식:", analysis.STRATEGIES[analysis.current_strategy()])
        print(f"이번 실행 DB 내려받은 양(대략): {db.read_mb():,.0f}MB  (무료 한도 월 5,000MB)")
        for line in analysis.calc_check(df):
            print(line)
    except Exception as e:  # noqa: BLE001
        print("점검 요약 실패:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="나라장터 데이터 수집")
    ap.add_argument("--days", type=int, default=7, help="최근 N일")
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--auto", action="store_true", help="최근 3일 + 과거 데이터 자동 채우기")
    ap.add_argument("--quick", action="store_true", help="어제·오늘 공고/기초금액/개찰결과만 빠르게")
    ap.add_argument("--scheduled", action="store_true",
                    help="예약 실행: 오늘 전체 수집이 안 됐으면 전체, 최근 2시간 반 안에 안 돌았으면 빠른 수집, 아니면 건너뜀")
    a = ap.parse_args()
    print("DB:", "원격(PostgreSQL)" if db.is_remote() else db.url())
    if a.scheduled:
        # GitHub 예약 실행은 늦어지거나 빠지기도 해서 예약을 여러 번 걸어 두고, 여기서 실제로 돌지 정함
        import os
        now = datetime.now()
        last_full = db.get_kv("last_full_date") or ""
        if last_full != date.today().isoformat():
            # 기록이 없던 예전 실행도 인정: 오늘 '(자동)' 수집 기록이 있으면 전체 수집 완료로 봄
            done_today = db.query_df("SELECT COUNT(*) AS n FROM collect_log WHERE kind LIKE '%(자동)%' AND run_at >= :d",
                                     {"d": date.today().isoformat()})
            if int(done_today["n"].iloc[0]):
                last_full = date.today().isoformat()
        last_any = db.get_kv("last_collect_at") or ""
        recent = last_any and (now - datetime.fromisoformat(last_any)).total_seconds() < 2.5 * 3600
        if last_full != date.today().isoformat():
            a.auto = True
            print("예약 실행 → 오늘 전체 수집이 아직 안 돼서 전체 수집")
        elif not recent and now.weekday() < 5 and 8 <= now.hour < 19:
            a.quick = True
            print("예약 실행 → 빠른 수집")
        else:
            print(f"예약 실행 → 최근에 수집함({last_any[:16]}) → 건너뜀")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                    f.write("skip=true\n")
            raise SystemExit(0)
    if a.auto or a.quick:
        try:
            import notify
            ch = notify.channels()
            print("알림 채널:", ", ".join(ch) if ch else "없음 (GitHub Secrets 의 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 확인 필요)")
        except Exception as e:  # noqa: BLE001
            print("알림 채널 확인 실패:", e)
        auto() if a.auto else quick()
        db.set_kv("last_collect_at", datetime.now().isoformat(timespec="minutes"))
        if a.auto:
            db.set_kv("last_full_date", date.today().isoformat())
        print_status()
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
