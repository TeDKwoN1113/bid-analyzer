"""공고 점검: 공고번호를 주면 API 원본(공고·참가지역·면허제한)과 우리 시스템의 참가 가능 판단을 출력.

공개 정보(공고 내용)만 출력하고 회사 투찰 정보는 출력하지 않음.
사용: python inspect_bid.py R26BK01816770 R26BK01818339
"""
import json
import sys

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


if __name__ == "__main__":
    load_settings()
    for a in sys.argv[1:]:
        for x in a.replace(",", " ").split():
            n, _, o = x.partition("-")
            inspect(n.strip(), o.strip() or "000")
