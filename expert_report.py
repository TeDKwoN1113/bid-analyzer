"""전문가 분석 보고 (발주처·예가·경쟁사·참가업체) — 낙찰에 쓸 수 있는 규칙이 있는지 '미래 정보 없이' 확인.

공개 실행 기록에 남으므로 합계·통계만 출력하고, 경쟁사 이름은 가려서(첫 글자만) 출력.
사용: python expert_report.py
"""
import numpy as np
import pandas as pd

import analysis
import db

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)


def mask(name) -> str:
    s = str(name or "")
    return (s[:1] + "○" * min(4, max(len(s) - 1, 1))) if s else "-"


def eligible_done(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df["sajung"].notna() & df["open_date"].notna() & df["sajung"].between(97, 103)]
    return analysis.apply_filters(d, keywords=analysis.company_keywords(), eligible_only=True).sort_values("open_date")


def section(title: str) -> None:
    print("\n" + "=" * 8 + f" {title} " + "=" * 8)


# ---------------- 1. 발주처 분석 ----------------
def org_analysis(done: pd.DataFrame) -> None:
    section("1. 발주처 분석: 기관마다 사정율 습관이 있나")
    g_med = done["sajung"].median()
    t = done.groupby("org").agg(공고=("sajung", "size"), 사정율중앙=("sajung", "median"),
                                사분위폭=("sajung", lambda x: x.quantile(.75) - x.quantile(.25)),
                                참가중앙=("participants", "median"))
    t = t[t["공고"] >= 8].sort_values("공고", ascending=False)
    t["전체대비"] = (t["사정율중앙"] - g_med).round(3)
    print(f"우리 참가 가능 개찰 공고 {len(done):,}건 · 전체 사정율 중앙 {g_med:.3f}% · 8건 이상 기관 {len(t)}곳")
    print(t.head(15).round(3).to_string())
    # 예측 시험: 그 공고 이전 같은 기관 중앙값 vs 이전 전체 중앙값 — 어느 쪽이 실제에 더 가까웠나
    errs_org, errs_all, n = [], [], 0
    hist_all = []
    by_org: dict = {}
    for r in done.itertuples():
        prev_org = by_org.get(r.org, [])
        if len(prev_org) >= 5 and len(hist_all) >= 50:
            errs_org.append(abs(r.sajung - np.median(prev_org[-30:])))
            errs_all.append(abs(r.sajung - np.median(hist_all[-300:])))
            n += 1
        by_org.setdefault(r.org, []).append(r.sajung)
        hist_all.append(r.sajung)
    if n:
        eo, ea = np.median(errs_org), np.median(errs_all)
        better = np.mean(np.array(errs_org) < np.array(errs_all)) * 100
        print(f"\n예측 시험 {n}건: 같은 기관 과거 중앙 오차 {eo:.3f}%p vs 전체 과거 중앙 오차 {ea:.3f}%p "
              f"(기관 쪽이 더 가까운 경우 {better:.0f}%)")
        print("→ 해석: 기관 오차가 확실히 작으면 '기관 습관'이 있음. 비슷하면 사정율은 기관과 상관없이 추첨으로 정해짐.")


# ---------------- 2. 예가 분석 ----------------
def prelim_analysis(done: pd.DataFrame) -> None:
    section("2. 예가 분석: 15개 예비가격 중 어떤 위치가 뽑히나")
    keys = list(done["bid_key"])
    pp = analysis._prelim(keys)
    if pp.empty:
        print("예비가격 상세 없음")
        return
    base = done.set_index("bid_key")["base_amt"]
    pp = pp[pp["price"] > 0].copy()
    pp["base"] = pp["bid_key"].map(base)
    pp = pp[pp["base"] > 0]
    pp["pct"] = pp["price"] / pp["base"] * 100 - 100
    cnt = pp.groupby("bid_key")["sno"].transform("size")
    pp = pp[cnt == 15]
    pp["가격순위"] = pp.groupby("bid_key")["price"].rank(method="first").astype(int)
    nb = pp["bid_key"].nunique()
    print(f"예비가격 15개가 모두 있는 공고 {nb:,}건 (최근 개찰분)")
    t = pp.groupby("가격순위").agg(뽑힌비율=("drawn", "mean"), 평균위치=("pct", "mean"))
    if "draw_count" in pp and pp["draw_count"].fillna(0).sum() > 0:
        t["업체선택수(평균)"] = pp.groupby("가격순위")["draw_count"].mean().round(1)
    t["뽑힌비율"] = (t["뽑힌비율"] * 100).round(1)
    t["평균위치"] = t["평균위치"].round(3)
    print("가격순위 1 = 15개 중 가장 낮은 예비가격 (균등이면 뽑힌비율 26.7%)")
    print(t.to_string())
    lo = pp[pp["가격순위"] <= 5]["drawn"].mean() * 100
    hi = pp[pp["가격순위"] >= 11]["drawn"].mean() * 100
    print(f"낮은 5개 뽑힌비율 {lo:.1f}% · 높은 5개 {hi:.1f}% (균등 26.7%)")
    # 범위(±2%/±3%)별 실제 사정율 퍼짐
    if "range_begin" in done:
        rng = (done["range_end"].abs().fillna(0) + done["range_begin"].abs().fillna(0)).round(1)
        t2 = done.groupby(rng).agg(공고=("sajung", "size"), 중앙=("sajung", "median"),
                                   표준편차=("sajung", "std"), 하위10=("sajung", lambda x: x.quantile(.1)),
                                   상위90=("sajung", lambda x: x.quantile(.9)))
        print("\n예가 범위(하한~상한 폭 %p)별 실제 사정율")
        print(t2[t2["공고"] >= 10].round(3).to_string())


# ---------------- 3. 경쟁사 분석 ----------------
def competitor_analysis(done: pd.DataFrame) -> None:
    section("3. 경쟁사 분석: 울산 공고에 누가 들어오고 어디에 몰리나")
    region = db.company()["region"] or "울산"
    loc = analysis.local_region_history(done, region)
    rk = analysis.load_ranks(loc["bid_key"])
    if rk.empty:
        print("개찰순위 자료 없음")
        return
    rk = rk[rk["투찰사정율"].notna()]
    nb = rk["bid_key"].nunique()
    print(f"{region} 지역제한 공고 {len(loc):,}건 중 개찰순위(상위 100) 있는 공고 {nb:,}건 · 업체 {rk['bizno'].nunique():,}곳")
    act = loc.set_index("bid_key")["sajung"]
    rk["실제"] = rk["bid_key"].map(act)
    rk["차이"] = rk["투찰사정율"] - rk["실제"]
    g = rk.groupby("bizno").agg(업체=("company", "first"), 등장=("bid_key", "nunique"), 일위=("rank", lambda x: int((x == 1).sum())),
                                십위내=("rank", lambda x: int((x <= 10).sum())), 노린사정율=("투찰사정율", "median"),
                                편차=("투찰사정율", "std"))
    g = g.sort_values("등장", ascending=False)
    g["업체"] = g["업체"].map(mask)
    g["10위내율"] = (g["십위내"] / g["등장"] * 100).round(0)
    print("자주 나오는 업체 (상위 100 안에 든 횟수 기준, 이름 가림)")
    print(g.head(12).round(3).to_string(index=False))
    top = g[g["등장"] >= 10]
    if len(top):
        print(f"10번 이상 나온 업체 {len(top)}곳 · 그중 1위 1번 이상 {int((top['일위'] > 0).sum())}곳 · "
              f"1위 합계 {int(top['일위'].sum())} / 공고 {nb}")
    # 경쟁사 투찰이 몰리는 사정율 (0.05%p 구간) — 몰린 곳 바로 위는 낙찰 구간이 좁음
    bins = np.arange(98.5, 101.55, 0.05)
    h, _ = np.histogram(rk["투찰사정율"], bins=bins)
    share = h / max(h.sum(), 1) * 100
    dens = pd.DataFrame({"구간": [f"{b:.2f}~{b + 0.05:.2f}" for b in bins[:-1]], "투찰비율(%)": share.round(2)})
    a_h, _ = np.histogram(loc["sajung"], bins=bins)
    dens["실제사정율비율(%)"] = (a_h / max(a_h.sum(), 1) * 100).round(2)
    dens["실제/투찰"] = (dens["실제사정율비율(%)"] / dens["투찰비율(%)"].replace(0, np.nan)).round(2)
    mid = dens.iloc[(len(dens) // 2) - 16:(len(dens) // 2) + 16]
    print("\n사정율 구간별: 경쟁사 투찰 비율 vs 실제 사정율이 나온 비율 (실제/투찰 > 1 이면 '덜 붐비는데 자주 나오는 곳')")
    print(mid.to_string(index=False))


# ---------------- 4. 참가업체 수 ----------------
def participant_analysis(done: pd.DataFrame) -> None:
    section("4. 참가업체 수: 경쟁이 적은 공고는 어떤 공고인가")
    amt = done["base_amt"].fillna(done["est_price"])
    region = db.company()["region"] or "울산"
    loc = done["bid_key"].isin(analysis.local_region_history(done, region)["bid_key"])
    t = done.assign(범위=np.where(loc, f"{region} 지역제한", "전국·기타"),
                    금액=pd.cut(amt, [0, 5e7, 1e8, 3e8, 1e9, 1e13], labels=["~5천만", "5천~1억", "1~3억", "3~10억", "10억~"]))
    p = t.groupby(["범위", "금액"], observed=True).agg(공고=("participants", "size"), 참가중앙=("participants", "median"),
                                                    공고당무작위확률=("participants", lambda x: (1 / x[x > 0]).mean() * 100))
    print(p.round(2).to_string())
    print("공고당무작위확률(%) = 아무렇게나 넣어도 1순위가 될 확률. 이 값이 큰 공고일수록 같은 노력으로 더 많이 이김.")
    few = small_org_bids()
    if len(few):
        print(f"\n'소수 참가 기관'으로 제외되는 공고 {len(few)}건 — 기관 목록 (설정의 '참가 가능 기관'으로 되살릴 수 있음)")
        t = few.groupby("org").agg(공고=("bid_key", "size"), 참가중앙=("participants", "median"),
                                   기초중앙_백만=("base_amt", lambda x: x.median() / 1e6),
                                   계약방법=("contract_method", lambda x: x.mode().iat[0] if len(x.mode()) else ""),
                                   공사지역=("region", lambda x: str(x.mode().iat[0])[:12] if len(x.mode()) else ""))
        t["기초중앙_백만"] = t["기초중앙_백만"].round(0)
        print(t.sort_values("공고", ascending=False).head(40).to_string())
        loc = few[few["region"].fillna("").str.contains(db.company()["region"] or "울산", regex=False)]
        print(f"그중 공사지역이 우리 지역인 공고 {len(loc)}건 · 기관:", ", ".join(loc["org"].value_counts().head(15).index))
        print("예시 공고명:", " / ".join(few.sort_values("open_date", ascending=False)["name"].astype(str).str[:30].head(8)))


# ---------------- 5. 적격심사 ----------------
def qualification_analysis(done: pd.DataFrame) -> None:
    section("5. 적격심사: 1순위가 심사에서 떨어지는 공고는?")
    d = done[done["first_won"].notna()].copy()
    if d.empty:
        print("1순위·최종 낙찰자 둘 다 있는 공고 없음")
        return
    d = d[~analysis.lowest_price(d)]
    amt = d["est_price"].fillna(d["base_amt"] / 1.1)
    d["금액"] = pd.cut(amt, [0, 1e8, 3e8, 1e9, 5e9, 1e13], labels=["~1억", "1~3억", "3~10억", "10~50억", "50억~"])
    d["탈락"] = 1 - d["first_won"]
    print(f"1순위·최종 낙찰자 모두 있는 공고 {len(d):,}건 · 1순위가 그대로 낙찰 {d['first_won'].mean() * 100:.1f}%")
    t = d.groupby("금액", observed=True).agg(공고=("탈락", "size"), 일순위탈락률=("탈락", "mean"), 참가중앙=("participants", "median"))
    t["일순위탈락률"] = (t["일순위탈락률"] * 100).round(1)
    print(t.to_string())
    m = d.groupby(d["method"].fillna("?").str.slice(0, 18)).agg(공고=("탈락", "size"), 일순위탈락률=("탈락", "mean"))
    m = m[m["공고"] >= 10]
    m["일순위탈락률"] = (m["일순위탈락률"] * 100).round(1)
    print(m.sort_values("공고", ascending=False).head(10).to_string())
    # 1순위가 떨어졌을 때 최종 낙찰자는 몇 순위였나 (개찰순위가 있는 공고)
    fail = d[d["탈락"] == 1]
    rk = analysis.load_ranks(fail["bid_key"])
    if len(rk):
        wb = fail.set_index("bid_key")["winner_bizno"].astype(str).str.replace("-", "")
        rk["wb"] = rk["bid_key"].map(wb)
        hit = rk[rk["bizno"].astype(str).str.replace("-", "") == rk["wb"]]
        if len(hit):
            print("1순위 탈락 시 최종 낙찰 순위:", hit["rank"].value_counts().sort_index().head(10).to_dict(),
                  f"(중앙 {hit['rank'].median():.0f}위)")
    try:
        lim = float(db.get_kv("qual_max_est", "") or 0)
    except ValueError:
        lim = 0
    if lim:
        over = (amt > lim).mean() * 100
        print(f"회사 적격심사 한도(추정가격 {lim / 1e8:.1f}억) 넘는 공고 비율 {over:.1f}%")


def small_org_bids() -> pd.DataFrame:
    df = analysis.load_dataset()
    d = analysis.apply_filters(df[df["open_date"].notna()], keywords=analysis.company_keywords())
    r = analysis.limit_reasons(d)
    return d[r.str.contains("소수 참가", regex=False)]


def main() -> None:
    df = analysis.load_dataset()
    done = eligible_done(df)
    if done.empty:
        print("분석할 공고 없음")
        return
    for f in (qualification_analysis, org_analysis, prelim_analysis, competitor_analysis, participant_analysis):
        try:
            f(done)
        except Exception as e:  # noqa: BLE001
            print(f"[{f.__name__} 실패] {e}")


if __name__ == "__main__":
    main()
