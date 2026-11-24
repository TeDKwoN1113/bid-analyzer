"""공고 점검: 공고번호를 주면 API 원본(공고·참가지역·면허제한)과 우리 시스템의 참가 가능 판단을 출력.

공개 정보(공고 내용)만 출력하고 회사 투찰 정보는 출력하지 않음.
사용: python inspect_bid.py R26BK01816770 R26BK01818339
"""
import json
import sys

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


if __name__ == "__main__":
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
