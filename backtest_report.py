"""추천 방식 공정 시험(백테스트) 보고: 각 공고마다 '그 공고 이전 자료만'으로 사정율을 골랐다면 1순위였는지.

무작위로 넣었을 때 기대값(1/참가업체수 합)과 비교하고, 우연일 확률(포아송)도 함께 출력.
공개 실행 기록에 남으므로 공고명·금액 등 개별 내용은 출력하지 않고 합계만 출력.
사용: python backtest_report.py [시험 일수=270] [최대 공고=800]
"""
import math
import sys

import pandas as pd

import analysis


def p_at_least(k: int, lam: float) -> float:
    """무작위 기대 lam 일 때 k건 이상 나올 확률."""
    if lam <= 0:
        return float("nan")
    return 1 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))


def main(days: int = 270, max_tests: int = 800) -> None:
    df = analysis.load_dataset()
    summary, detail = analysis.backtest(df, analysis.company_keywords(), test_days=days, max_tests=max_tests,
                                        eligible_only=True)
    if summary.empty:
        print("시험할 공고 없음")
        return
    summary["우연일 확률(%)"] = [round(p_at_least(int(k), float(l)) * 100, 1)
                              for k, l in zip(summary["1순위"], summary["무작위 기대 1순위"])]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(f"=== 공정 시험: 최근 {days}일 우리 참가 가능·개찰 완료 공고 (최대 {max_tests}건) ===")
    print(summary.to_string(index=False))
    # 시기별(앞/뒤 절반)로 나눠도 같은 결론인지
    if len(detail):
        mid = detail["개찰일"].median()
        for name, part in (("앞 절반", detail[detail["개찰일"] < mid]), ("뒤 절반", detail[detail["개찰일"] >= mid])):
            g = part.groupby("방식")
            t = pd.DataFrame({"공고": g.size(), "1순위": g["결과"].apply(lambda x: int((x == "1순위").sum())),
                              "무작위 기대": g["무작위 기대"].sum().round(2),
                              "하한 미달률(%)": g["결과"].apply(lambda x: round((x == "하한 미달").mean() * 100, 1))})
            print(f"\n--- {name} ---")
            print(t.to_string())
        # ---- 경쟁 공고(참가 200곳 이상)만: 우리가 실제로 넣는 공고 유형 ----
        pmap = df.set_index("bid_key")["participants"]
        c = detail.assign(p=detail["bid_key"].map(pmap))
        c = c[c["p"] >= 200]
        if len(c):
            g = c.groupby("방식")
            t = pd.DataFrame({"공고": g.size(), "1순위": g["결과"].apply(lambda x: int((x == "1순위").sum())),
                              "무작위 기대": g["무작위 기대"].sum().round(2),
                              "하한 미달률(%)": g["결과"].apply(lambda x: round((x == "하한 미달").mean() * 100, 1)),
                              "추천-실제(중앙)": (g["추천 사정율"].median() - 0).round(2)})
            t["추천-실제(중앙)"] = c.assign(d=c["추천 사정율"] - c["실제 사정율"]).groupby("방식")["d"].median().round(3)
            if "가상 순위" in c:
                t["10위 이내"] = g["가상 순위"].apply(lambda x: int((x <= 10).sum()))
                t["50위 이내"] = g["가상 순위"].apply(lambda x: int((x <= 50).sum()))
            print(f"\n=== 경쟁 공고(참가 200곳 이상) {c['bid_key'].nunique()}건만 ===")
            print(t.sort_values(["1순위", "10위 이내"] if "10위 이내" in t else ["1순위"], ascending=False).to_string())
        # ---- 어디서 이기나: 공고 특성별 (현재 방식 기준) ----
        cur = analysis.STRATEGIES[analysis.current_strategy()]
        cols = [c for c in ("bid_key", "participants", "base_amt", "est_price", "contract_method", "method", "org",
                            "lower_rate", "allowed_regions", "region") if c in df]
        x = detail[detail["방식"] == cur].merge(df[cols], on="bid_key", how="left")
        x["win"] = (x["결과"] == "1순위").astype(int)
        x["low"] = (x["결과"] == "하한 미달").astype(int)
        amt = x["base_amt"].fillna(x["est_price"])
        groups = {
            "참가업체수": pd.cut(x["participants"], [0, 30, 100, 200, 400, 800, 1e6],
                             labels=["~30", "31~100", "101~200", "201~400", "401~800", "800~"]),
            "금액(기초)": pd.cut(amt, [0, 5e7, 1e8, 3e8, 1e9, 1e13], labels=["~5천만", "5천~1억", "1~3억", "3~10억", "10억~"]),
            "하한율": x["lower_rate"].round(3).astype(str).where(x["lower_rate"].notna(), "?"),
            "지역제한": (x["allowed_regions"].fillna("") != "").map({True: "지역제한", False: "전국"}),
            "낙찰방법": x["method"].fillna("?").str.slice(0, 14),
        }
        print(f"\n=== 어디서 이기나 ({cur}, 시험 {len(x)}건) — 무작위 대비 = 1순위 ÷ 무작위 기대 ===")
        for gname, key in groups.items():
            t = x.groupby(key, observed=True).agg(공고=("win", "size"), 순위1=("win", "sum"), 무작위기대=("무작위 기대", "sum"),
                                                  하한미달률=("low", "mean"), 참가중앙=("participants", "median"))
            t = t[t["공고"] >= 10]
            t["무작위기대"] = t["무작위기대"].round(2)
            t["무작위대비"] = (t["순위1"] / t["무작위기대"]).round(2)
            t["하한미달률"] = (t["하한미달률"] * 100).round(0)
            print(f"\n[{gname}]")
            print(t.sort_values("공고", ascending=False).head(10).to_string())
        # 추천 사정율과 실제 사정율 차이 (치우침 점검)
        d = detail.assign(차이=detail["추천 사정율"] - detail["실제 사정율"])
        print("\n--- 추천 - 실제 사정율 (중앙, %p) ---")
        print(d.groupby("방식")["차이"].median().round(3).to_string())


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:3]]
    main(*a)
