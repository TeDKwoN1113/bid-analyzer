"""분석 로직: 사정율, 1순위 사정율, 투찰금액 계산."""
import math

import numpy as np
import pandas as pd

import db
from config import load_settings


def load_dataset() -> pd.DataFrame:
    """공고 + 기초금액 + 예정가격 + 낙찰결과를 공고 단위로 합친 표."""
    con = db.connect()
    df = pd.read_sql_query(
        """
        SELECT b.bid_key, b.bid_no, b.bid_ord, b.name, b.org, b.demand_org, b.region,
               b.main_type, b.method, b.notice_dt, b.open_dt, b.est_price, b.lower_rate, b.url,
               ba.base_amt AS base_amt_notice, ba.a_value, ba.range_begin, ba.range_end,
               p.plan_price, p.base_amt AS base_amt_open,
               r.participants, r.winner, r.win_amt, r.win_rate
        FROM bids b
        LEFT JOIN base_amounts ba ON ba.bid_key = b.bid_key
        LEFT JOIN (SELECT bid_key, MAX(plan_price) plan_price, MAX(base_amt) base_amt
                   FROM prelim_prices GROUP BY bid_key) p ON p.bid_key = b.bid_key
        LEFT JOIN results r ON r.bid_key = b.bid_key
        """,
        con,
    )
    con.close()
    if df.empty:
        return df
    df["base_amt"] = df["base_amt_open"].fillna(df["base_amt_notice"])
    df["a_value"] = df["a_value"].fillna(0)
    # 사정율(%) = 예정가격 / 기초금액
    df["sajung"] = df["plan_price"] / df["base_amt"] * 100
    # 1순위(낙찰자) 사정율: 낙찰자가 '노린' 사정율 = 낙찰금액을 하한율로 역산한 예정가격 / 기초금액
    r = df["lower_rate"] / 100
    implied_plan = (df["win_amt"] - df["a_value"]) / r + df["a_value"]
    df["winner_sajung"] = implied_plan / df["base_amt"] * 100
    df["open_date"] = pd.to_datetime(df["open_dt"].astype(str).str[:10], errors="coerce")
    # 이상치 제거 (데이터 오류 방지)
    df.loc[(df["sajung"] < 90) | (df["sajung"] > 110), "sajung"] = np.nan
    df.loc[(df["winner_sajung"] < 90) | (df["winner_sajung"] > 110), "winner_sajung"] = np.nan
    return df


def apply_filters(df: pd.DataFrame, keywords=None, regions=None, orgs=None,
                  min_amt=None, max_amt=None, date_from=None, date_to=None,
                  name_query=None) -> pd.DataFrame:
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if keywords:
        text = df["name"].fillna("") + " " + df["main_type"].fillna("")
        m &= text.str.contains("|".join(keywords), regex=True)
    if regions:
        text = df["region"].fillna("") + " " + df["org"].fillna("") + " " + df["demand_org"].fillna("")
        m &= text.str.contains("|".join(regions), regex=True)
    if orgs:
        m &= df["org"].isin(orgs)
    amt = df["base_amt"].fillna(df["est_price"])
    if min_amt:
        m &= amt >= min_amt
    if max_amt:
        m &= amt < max_amt
    if date_from is not None:
        m &= df["open_date"] >= pd.Timestamp(date_from)
    if date_to is not None:
        m &= df["open_date"] <= pd.Timestamp(date_to)
    if name_query:
        m &= df["name"].fillna("").str.contains(name_query, regex=False)
    return df[m]


def bid_amount(base_amt: float, sajung: float, lower_rate: float, a_value: float = 0) -> int:
    """투찰금액 = (예정가격 - A) × 하한율 + A, 원 단위 올림."""
    plan = base_amt * sajung / 100
    return int(math.ceil((plan - a_value) * lower_rate / 100 + a_value))


def sajung_table(hist: pd.DataFrame, base_amt: float, lower_rate: float, a_value: float = 0,
                 lo: float = 98.0, hi: float = 102.0, step: float = 0.1) -> pd.DataFrame:
    """사정율 후보별 투찰금액과 과거 통계."""
    actual = hist["sajung"].dropna().values
    winners = hist["winner_sajung"].dropna().values
    rows = []
    for s in np.round(np.arange(lo, hi + 1e-9, step), 3):
        valid = (actual >= s).mean() * 100 if len(actual) else np.nan
        near = ((winners >= s - step / 2) & (winners < s + step / 2)).sum() if len(winners) else 0
        rows.append({
            "사정율(%)": s,
            "투찰금액(원)": bid_amount(base_amt, s, lower_rate, a_value),
            "하한 통과확률(%)": round(valid, 1) if not np.isnan(valid) else None,
            "과거 1순위 빈도(건)": int(near),
        })
    return pd.DataFrame(rows)


def recommend(hist: pd.DataFrame, step: float = 0.1, top: int = 3) -> list[dict]:
    """과거 1순위 사정율이 가장 많이 몰린 구간 추천."""
    w = hist["winner_sajung"].dropna()
    if len(w) < 5:
        return []
    bins = (np.floor(w / step) * step).round(3)
    vc = bins.value_counts().head(top)
    actual = hist["sajung"].dropna()
    out = []
    for b, cnt in vc.items():
        out.append({
            "구간": f"{b:.1f} ~ {b + step:.1f}%",
            "1순위 건수": int(cnt),
            "비율(%)": round(cnt / len(w) * 100, 1),
            "하한 통과확률(%)": float(round((actual >= b).mean() * 100, 1)) if len(actual) else None,
            "추천 사정율": float(round(b + step / 2, 3)),
        })
    return out


def drawn_number_stats(bid_keys) -> pd.DataFrame:
    """예비가격 번호(1~15)별 추첨 빈도."""
    con = db.connect()
    keys = list(bid_keys)
    if not keys:
        return pd.DataFrame(columns=["번호", "추첨횟수"])
    q = ",".join("?" * len(keys))
    df = pd.read_sql_query(
        f"SELECT sno AS 번호, SUM(drawn) AS 추첨횟수, COUNT(*) AS 전체 "
        f"FROM prelim_prices WHERE bid_key IN ({q}) GROUP BY sno ORDER BY sno",
        con, params=keys)
    con.close()
    return df


def prelim_position_stats(bid_keys) -> pd.DataFrame:
    """예비가격 15개를 기초금액 대비 %로 바꿨을 때, 추첨된 가격들이 어느 위치에 있었나."""
    con = db.connect()
    keys = list(bid_keys)
    if not keys:
        return pd.DataFrame()
    q = ",".join("?" * len(keys))
    df = pd.read_sql_query(
        f"SELECT bid_key, sno, price, drawn, base_amt FROM prelim_prices WHERE bid_key IN ({q})",
        con, params=keys)
    con.close()
    df["pct"] = df["price"] / df["base_amt"] * 100
    return df


def competitor_stats(min_count: int = 2) -> pd.DataFrame:
    """개찰순위 데이터가 있는 공고들에서 업체별 투찰 경향."""
    con = db.connect()
    df = pd.read_sql_query(
        """SELECT k.company, k.bizno, k.rank, k.amount, k.rate, ba.base_amt, b.lower_rate
           FROM ranks k JOIN bids b ON b.bid_key = k.bid_key
           LEFT JOIN base_amounts ba ON ba.bid_key = k.bid_key""", con)
    con.close()
    if df.empty:
        return df
    df["투찰사정율"] = df["amount"] / (df["lower_rate"] / 100) / df["base_amt"] * 100
    agg = df.groupby("company").agg(**{
        "참가횟수": ("rank", "count"),
        "1순위횟수": ("rank", lambda x: int((x == 1).sum())),
        "평균투찰률": ("rate", "mean"),
        "평균투찰사정율": ("투찰사정율", "mean"),
        "사정율편차": ("투찰사정율", "std"),
    }).reset_index().rename(columns={"company": "업체명"})
    return agg[agg["참가횟수"] >= min_count].sort_values("참가횟수", ascending=False).round(3)


def default_keywords():
    return load_settings()["target_keywords"]
