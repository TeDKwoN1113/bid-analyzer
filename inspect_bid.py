"""공고 점검: 공고번호를 주면 API 원본(공고·참가지역·면허제한)과 우리 시스템의 참가 가능 판단을 출력.

공개 정보(공고 내용)만 출력하고 회사 투찰 정보는 출력하지 않음.
사용: python inspect_bid.py R26BK01816770 R26BK01818339
"""
import json
import sys

import numpy as np
import pandas as pd

import api_client
import db
from config import load_settings

KEYS_HINT = ("rgn", "Rgn", "lmt", "Lmt", "lcns", "Lcns", "prtcpt", "Prtcpt", "indstryty", "Indstryty",
             "cnstty", "Cnstty", "Yn", "cmpt", "Cmpt", "arslt", "pq", "dsgnt", "jntcontrct", "mainCnsttyNm", "bidNtceNm",
             "cntrctCnclsMthdNm", "presmptPrce", "sucsfbidLwltRate", "ntceKindNm")


def show(label, items, full=False):
    print(f"  {label}: {len(items)}건")
    for it in items[:5]:
        sub = it if full else {k: v for k, v in it.items() if any(h in k for h in KEYS_HINT) and v not in ("", None)}
        print("    ", json.dumps(sub, ensure_ascii=False)[:1500])


def inspect(no: str, ord_: str = "000") -> None:
    print(f"\n===== {no}-{ord_} =====")
    for op, divs in (("bid_list", ["2", "3", "1"]), ("regions", ["2", "1"]), ("licenses", ["2", "1"])):
        for div in divs:
            for o in dict.fromkeys([ord_, "000", "001"]):
                p = {"inqryDiv": div, "bidNtceNo": no, "bidNtceOrd": o}
                try:
                    items, total = api_client.call(op, p, rows=100)
                except Exception as e:  # noqa: BLE001
                    print(f"  {op} inqryDiv={div} ord={o}: 오류 {str(e)[:150]}")
                    continue
                mine = [x for x in items if str(x.get("bidNtceNo")) == no]
                print(f"  {op} inqryDiv={div} ord={o}: 응답 {len(items)}건(전체 {total}) · 이 공고 {len(mine)}건")
                if mine:
                    show(op, mine, full=(op != "bid_list"))
                    break
            else:
                continue
            break
    # 우리 시스템 판단
    try:
        import analysis
        df = analysis.load_dataset()
        r = df[df["bid_no"] == no]
        if r.empty:
            print("  [시스템] 분석 표에 없음")
        else:
            r = r.iloc[0]
            print("  [시스템] 참가지역:", repr(r.get("allowed_regions")), "확인됨:", r.get("allowed_regions_known"),
                  "| 면허:", repr(r.get("licenses")), "확인됨:", r.get("licenses_known"))
            print("  [시스템] 지역:", r.get("region"), "| 계약:", r.get("contract_method"), "| 실적:", r.get("perf_limit"),
                  "| PQ:", r.get("pq"), "| 지명:", r.get("designated"))
            print("  [시스템] 제한사유:", analysis.limit_reasons(df[df["bid_no"] == no]).iloc[0]
                  if hasattr(analysis, "limit_reasons") else "-")
            print("  [시스템] 참가 가능:", bool(analysis.eligible(df[df["bid_no"] == no]).iloc[0])
                  if hasattr(analysis, "eligible") else "-")
        chk = db.query_df("SELECT * FROM limit_checks WHERE bid_no = :b", {"b": no})
        print("  [시스템] 재확인 기록:", chk.to_dict("records"))
    except Exception as e:  # noqa: BLE001
        print("  [시스템] 판단 출력 실패:", e)


def region_selftest(n: int = 3) -> None:
    """지역제한이 '확실히' 있는 최근 공고(날짜별 수집으로 참가지역이 들어온 공고)를 골라 공고번호 조회도 같은 결과인지 확인."""
    q = db.query_df("""SELECT r.bid_key, COUNT(*) AS n FROM regions r JOIN bids b ON b.bid_key = r.bid_key
                       WHERE b.notice_dt >= :d GROUP BY r.bid_key ORDER BY r.bid_key DESC LIMIT :n""",
                    {"d": (pd.Timestamp.now() - pd.Timedelta(days=10)).strftime("%Y-%m-%d"), "n": n})
    print(f"지역제한 확실한 공고 {len(q)}건으로 공고번호 조회 시험")
    for k, cnt in zip(q["bid_key"], q["n"]):
        no, o = k.rsplit("-", 1)
        print(f"\n[{k}] DB에 저장된 참가지역 {cnt}건")
        for div in ("2", "1", "3"):
            try:
                items, total = api_client.call("regions", {"inqryDiv": div, "bidNtceNo": no, "bidNtceOrd": o}, rows=100)
                mine = [x for x in items if str(x.get("bidNtceNo")) == no]
                print(f"  inqryDiv={div}: 응답 {len(items)}건 · 이 공고 {len(mine)}건",
                      json.dumps(mine[:2], ensure_ascii=False)[:400] if mine else "")
            except Exception as e:  # noqa: BLE001
                print(f"  inqryDiv={div}: 오류 {str(e)[:150]}")


def d2b_selftest(n: int = 4) -> None:
    """국방 10억 미만 공사 공고 몇 건의 상세에서 지역제한(areaLmttList)이 실제로 오는지, 우리 시스템 판단은 어떤지."""
    from datetime import date, timedelta

    import d2b
    lst, _ = d2b.call(d2b.BID, "getFcltyCmpetBidPblancList",
                      {"anmtDateBegin": d2b._d(date.today() - timedelta(days=20)), "anmtDateEnd": d2b._d(date.today())},
                      rows=200)
    small = [x for x in lst if (d2b._num(x.get("baseAmnt")) or 0) < 1.1e9]
    big = [x for x in lst if (d2b._num(x.get("baseAmnt")) or 0) >= 1.1e9]
    print(f"국방 공고 최근 20일 {len(lst)}건 (10억 미만 {len(small)} · 이상 {len(big)})")
    import analysis
    df = analysis.load_dataset()
    for x in small[:n] + big[:2]:
        p_ = {k: x.get(k) for k in ("pblancYear", "pblancSeCode", "pblancNo", "pblancOdr", "cntrwkNo", "orntCode")}
        try:
            det = (d2b.call(d2b.BID, "getFcltyCmpetBidPblancDetail", p_)[0] or [{}])[0]
        except Exception as e:  # noqa: BLE001
            print("  상세 오류", str(e)[:120])
            continue
        if x is small[0]:
            print("  [목록 항목 키]", sorted(x.keys()))
            print("  [상세 요청]", p_)
            try:
                import requests
                from config import load_settings as _ls
                rr = requests.get(f"{d2b.BID}/getFcltyCmpetBidPblancDetail",
                                  params={"serviceKey": _ls()["service_key"], "type": "json", "_type": "json",
                                          "pageNo": 1, "numOfRows": 10, **{k: v for k, v in p_.items() if v}}, timeout=40)
                print("  [상세 원본]", rr.text[:1500].replace(_ls()["service_key"], "***"))
            except Exception as e:  # noqa: BLE001
                print("  [상세 원본 오류]", e)
        print("  [상세 항목 수]", len(det), sorted(det.keys())[:60])
        key = d2b.keys(x)[0] if hasattr(d2b, "keys") else ""
        print(f"\n[{x.get('cntrwkNm')}] 기초 {d2b._num(x.get('baseAmnt')) or 0:,.0f} · 공사지역 {det.get('cntrwkLc') or det.get('lc')}")
        print("  areaLmttList:", repr(det.get("areaLmttList"))[:300], "| lcnsLmttList:", repr(det.get("lcnsLmttList"))[:200])
        print("  지역관련 항목:", {k: v for k, v in det.items() if any(h in k.lower() for h in ("area", "rgn", "lc", "lmt"))
                              and v not in ("", None)})
        r = df[df["bid_key"].astype(str).str.startswith(str(key).rsplit("-", 1)[0])] if key else df.iloc[0:0]
        if len(r):
            r0 = r.iloc[0]
            print("  [시스템] 참가지역:", repr(r0.get("allowed_regions")), "확인됨:", r0.get("allowed_regions_known"),
                  "| 참가 가능:", bool(analysis.eligible(r.head(1)).iloc[0]))
        else:
            print("  [시스템] 저장 안 됨(우리 업종 아님 또는 아직 수집 전)")


def eligibility_summary() -> None:
    """개찰 전·우리 업종 공고가 어떤 이유로 빠지는지 (넣을 수 있는 공고를 잘못 버리지 않는지 점검)."""
    import analysis
    df = analysis.load_dataset()
    now = pd.Timestamp.now()
    up = analysis.apply_filters(df[df["open_date"] >= now], keywords=analysis.company_keywords())
    ok = analysis.eligible(up)
    reasons = analysis.limit_reasons(up)
    reg = db.company()["region"] or "울산"
    print(f"개찰 전 우리 업종 공고 {len(up)}건 → 참가 가능 {int(ok.sum())}건 · 제외 {int((~ok).sum())}건")
    ex = up[~ok].copy()
    ex["사유"] = reasons[~ok].replace("", np.nan)
    allowed = ex["allowed_regions"].fillna("")
    ex.loc[ex["사유"].isna() & (allowed != "") & ~allowed.str.contains(reg, regex=False), "사유"] = "지역제한(다른 지역)"
    ex.loc[ex["사유"].isna() & ex["method"].fillna("").str.contains("수의시담"), "사유"] = "수의시담"
    ex.loc[ex["사유"].isna() & ~ex["allowed_regions_known"].astype(bool), "사유"] = "지역정보 아직 없음(현장이 다른 지역)"
    ex["사유"] = ex["사유"].fillna("면허 불일치/기타")
    for why, g in ex.groupby(ex["사유"].str.split(" · ").str[0]):
        print(f"\n[{why}] {len(g)}건 — 예:")
        for r in g.sort_values("open_date").head(6).itertuples():
            print(f"   · {str(r.name)[:40]} | {str(r.org)[:16]} | 지역 {str(r.allowed_regions or '-')[:20]} | "
                  f"면허 {str(r.licenses or '-')[:40]} | {str(r.contract_method or '')[:8]} | {r.bid_no}")
    lp = analysis.lowest_price(up[ok])
    print(f"\n참가 가능 중 최저가 내역서 입찰 {int(lp.sum())}건, ⭐ 후보 계산은 사이트에서")


def deep(no: str) -> None:
    """공고 하나 집중 분석 (공개 공고 정보만 — 회사 투찰 내용은 출력하지 않음)."""
    import analysis
    df = analysis.load_dataset()
    rows = df[df["bid_no"] == no]
    if rows.empty:
        print(f"{no}: 분석 표에 없음")
        return
    r = rows.iloc[0]
    print(f"\n######## 집중 분석: {r['name']} ({no}) ########")
    for k in ("org", "demand_org", "region", "contract_method", "method", "open_dt", "close_dt", "est_price", "base_amt",
              "lower_rate", "a_value", "pure_cost", "range_begin", "range_end", "joint_duty", "joint_rate", "detail_bid",
              "ability_list", "allowed_regions", "licenses", "participants"):
        if k in r.index:
            print(f"  {k}: {r[k]}")
    print("  참가지역 표시:", analysis.region_label(r), "| 참가 가능:", bool(analysis.eligible(rows.head(1)).iloc[0]),
          "| 제한사유:", analysis.limit_reasons(rows.head(1)).iloc[0] or "-")
    print("  적격심사 자가진단:", analysis.qual_check(r))
    print("  같은 유형(지역범위·계약방식·금액대) 보통 참가:", analysis.expected_competition(df, r), "| 유형:", analysis._comp_group(r))
    kw = analysis.bid_keywords(r)
    # 지역의무공동도급·큰 금액 공고는 실적·협정 문턱 때문에 참가가 적다는 대표 의견 → 과거 실제 참가 수 확인
    done_all = df[df["participants"] > 0]
    amt_all = done_all["est_price"].fillna(done_all["base_amt"] / 1.1)
    jd = done_all[(done_all["joint_duty"].fillna("") == "Y") & (amt_all >= 1e9)]
    if len(jd):
        loc = jd[jd["region"].fillna("").str.contains("울산", regex=False)]
        print(f"  과거 '지역의무공동도급 + 추정 10억↑' 공고 {len(jd)}건 참가 중앙 {jd['participants'].median():.0f}곳 "
              f"(25~75%: {jd['participants'].quantile(.25):.0f}~{jd['participants'].quantile(.75):.0f}) · "
              f"그중 울산 현장 {len(loc)}건 참가 중앙 {loc['participants'].median() if len(loc) else float('nan'):.0f}곳")
    big = done_all[(amt_all >= 1e9) & (amt_all < 5e9) & done_all["region"].fillna("").str.contains("울산", regex=False)]
    if len(big):
        print(f"  과거 울산 현장 추정 10~50억 공고 {len(big)}건 참가 중앙 {big['participants'].median():.0f}곳")
    # 비교 공고 단계별 사정율 분포
    for name, h in analysis.tiers(df, r, kw):
        s_ = h["sajung"].dropna()
        w_ = (h["winner_sajung"] - h["sajung"]).dropna()
        if len(s_):
            print(f"  [{name}] {len(s_)}건 · 실제 사정율 10/25/50/75/90% = "
                  + " / ".join(f"{s_.quantile(q):.3f}" for q in (.1, .25, .5, .75, .9))
                  + f" · 낙찰구간 폭 중앙 {w_[w_ >= 0].median():.4f}%p · 참가 중앙 {h['participants'].median():.0f}")
    if pd.notna(r.get("base_amt")) and pd.notna(r.get("lower_rate")):
        for mode in ("blend", "smooth30", "mean_sajung"):
            pk = analysis.pick_bid(df, r, kw, mode=mode)
            if pk:
                print(f"\n  ▶ 방식 {analysis.STRATEGIES[mode]}: 사정율 {pk['사정율']:.3f}% · 투찰금액 {pk['투찰금액']:,}원 · "
                      f"낙찰확률 {pk.get('낙찰확률')}% · 무작위 대비 {pk.get('무작위 대비')} · 신뢰도 {pk['신뢰도']}")
                for c in pk.get("후보", [])[:3]:
                    print("     후보:", c)
                if mode == "blend":
                    for x in analysis.evidence_lines(pk)[:8]:
                        print("     ·", x)
        # 참가가 적을 때(50~250곳) 과거 공고만으로 계산한 추천 — 이 공고처럼 문턱이 있는 경우 대비
        h0 = analysis._context(df, kw, db.company()["region"])["ind"]   # 참가 수로 거르기 전 같은 업종 전체
        for lo_, hi_ in ((50, 250), (100, 400)):
            hh = h0[h0["participants"].between(lo_, hi_)]
            if analysis.n_windows(hh) >= 20:
                for wd in (None, 0.3):
                    cv = analysis.win_curve(hh, width=wd)
                    pts = analysis.best_points(cv, top=3)
                    if pts:
                        p0 = pts[0]
                        amt = analysis.bid_amount(r["base_amt"], p0["사정율"], r["lower_rate"], r["a_value"] or 0)
                        print(f"\n  ▶ 참가 {lo_}~{hi_}곳 공고 {len(hh)}건 기준{' (넓게 평활)' if wd else ''}: 사정율 "
                              f"{p0['사정율']:.3f}% · 투찰금액 {amt:,}원 · 과거 낙찰확률 {p0['낙찰확률']:.2f}% · 다른 후보 "
                              + ", ".join(f"{x['사정율']:.2f}%({x['낙찰확률']:.2f}%)" for x in pts[1:]))
                s_ = hh["sajung"].dropna()
                print(f"     실제 사정율 10/25/50/75/90% = " + " / ".join(f"{s_.quantile(q):.3f}" for q in (.1, .25, .5, .75, .9))
                      + f" · 낙찰구간 폭 중앙 {(hh['winner_sajung'] - hh['sajung']).clip(lower=0).median():.4f}%p")
        try:
            rk = analysis.load_ranks()
            lab, comp = analysis.likely_competitors(rk, r, df)
            if len(comp):
                comp = comp.copy()
                for c in comp.columns:
                    if "업체" in c:
                        comp[c] = comp[c].map(lambda s: str(s)[:1] + "○○○")
                print(f"\n  예상 경쟁사 ({lab}) — 이름 가림")
                print(comp.head(8).to_string(index=False))
        except Exception as e:  # noqa: BLE001
            print("  경쟁사 분석 실패:", e)
    else:
        print("  기초금액(또는 하한율) 미공개 → 추천 금액은 공개 후 계산")


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1].strip().upper().startswith("DEEP:"):
        load_settings()
        for x in sys.argv[1].split(":", 1)[1].replace(",", " ").split():
            deep(x.strip())
        sys.exit(0)
    if sys.argv[1:] and sys.argv[1].strip().upper() == "ELIG":
        load_settings()
        eligibility_summary()
        sys.exit(0)
    if sys.argv[1:] and sys.argv[1].strip().upper() == "KEPCO":
        load_settings()
        print("한전 마지막으로 받은 때:", db.get_kv("kepco_fetched_at"))
        print("한전 마지막 결과:", db.get_kv("kepco_fetch_result"))
        print(db.query_df("SELECT COUNT(*) AS 공고, MAX(notice_dt) AS 최근공고일 FROM kepco_bids").to_string(index=False))
        sys.exit(0)
    if sys.argv[1:] and sys.argv[1].strip().upper() == "D2BTEST":
        load_settings()
        d2b_selftest()
        sys.exit(0)
    load_settings()
    if sys.argv[1:] and sys.argv[1].strip().upper() == "REGIONTEST":
        region_selftest()
        sys.exit(0)
    for a in sys.argv[1:]:
        for x in a.replace(",", " ").split():
            n, _, o = x.partition("-")
            inspect(n.strip(), o.strip() or "000")
