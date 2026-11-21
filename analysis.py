"""분석 로직: 사정율, 1순위 사정율, 투찰금액 계산."""
import math

import numpy as np
import pandas as pd

import db
from config import load_settings


def load_dataset() -> pd.DataFrame:
    """공고 + 기초금액 + 예정가격 + 개찰결과 + 낙찰자를 공고 단위로 합친 표."""
    df = db.query_df(
        """
        SELECT b.bid_key, b.bid_no, b.bid_ord, b.name, b.org, b.demand_org, b.region,
               b.main_type, b.method, b.contract_method, b.notice_dt, b.open_dt, b.est_price,
               b.lower_rate, b.url,
               ba.base_amt AS base_amt_notice, ba.a_value, ba.range_begin, ba.range_end,
               p.plan_price, p.base_amt AS base_amt_open,
               o.participants AS participants_open, o.status, o.first_company, o.first_amt,
               r.participants AS participants_final, r.winner, r.win_amt, r.win_rate
        FROM bids b
        LEFT JOIN base_amounts ba ON ba.bid_key = b.bid_key
        LEFT JOIN plans p ON p.bid_key = b.bid_key
        LEFT JOIN openings o ON o.bid_key = b.bid_key
        LEFT JOIN results r ON r.bid_key = b.bid_key
        """
    )
    if df.empty:
        return df
    df["base_amt"] = df["base_amt_open"].fillna(df["base_amt_notice"])
    df["a_value"] = df["a_value"].fillna(0)
    df["participants"] = df["participants_open"].where(df["participants_open"] > 0,
                                                       df["participants_final"])
    # 1순위 = 개찰 1순위(개찰 직후 공개), 없으면 최종 낙찰자
    df["first_company"] = df["first_company"].fillna(df["winner"])
    df["first_amt"] = df["first_amt"].fillna(df["win_amt"])
    # 사정율(%) = 예정가격 / 기초금액
    df["sajung"] = df["plan_price"] / df["base_amt"] * 100
    # 1순위(낙찰자) 사정율: 낙찰자가 '노린' 사정율 = 낙찰금액을 하한율로 역산한 예정가격 / 기초금액
    r = df["lower_rate"] / 100
    implied_plan = (df["first_amt"] - df["a_value"]) / r + df["a_value"]
    df["winner_sajung"] = implied_plan / df["base_amt"] * 100
    df["open_date"] = pd.to_datetime(df["open_dt"], errors="coerce", format="mixed")
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


def win_windows(hist: pd.DataFrame) -> pd.DataFrame:
    """과거 공고별 '낙찰 구간' [실제 사정율, 1순위 사정율).

    내 투찰 사정율 s 가 이 구간 안에 있었다면:
      - s ≥ 실제 사정율 → 투찰금액이 하한 이상(유효)
      - s < 1순위 사정율 → 실제 1순위보다 낮은 금액 → 내가 1순위
    """
    h = hist.dropna(subset=["sajung", "winner_sajung"])
    h = h[h["winner_sajung"] >= h["sajung"]]  # 데이터 오류 제거
    return h[["bid_key", "sajung", "winner_sajung"]]


def win_curve(hist: pd.DataFrame, lo: float = 98.0, hi: float = 102.0, step: float = 0.01) -> pd.DataFrame:
    """사정율별 과거 낙찰확률·하한통과확률."""
    w = win_windows(hist)
    grid = np.round(np.arange(lo, hi + 1e-9, step), 4)
    if w.empty:
        return pd.DataFrame({"사정율": grid, "낙찰확률": np.nan, "낙찰확률(원값)": np.nan,
                             "하한통과확률": np.nan, "표본": 0})
    a = w["sajung"].values[None, :]
    b = w["winner_sajung"].values[None, :]
    g = grid[:, None]
    win = ((g >= a) & (g < b)).mean(axis=1) * 100
    valid = (g >= a).mean(axis=1) * 100
    # 한 공고의 낙찰 구간은 매우 좁아서 그래프가 들쭉날쭉함 → ±0.05%p 범위 평균으로 평활화
    k = max(1, int(round(0.05 / step)))
    smooth = pd.Series(win).rolling(2 * k + 1, center=True, min_periods=1).mean().values
    return pd.DataFrame({"사정율": grid, "낙찰확률": smooth.round(2), "낙찰확률(원값)": win.round(2),
                         "하한통과확률": valid.round(1), "표본": len(w)})


def best_points(curve: pd.DataFrame, top: int = 3, min_gap: float = 0.15) -> list[dict]:
    """낙찰확률이 높은 사정율 상위 N개 (서로 min_gap 이상 떨어진 지점)."""
    if curve.empty or curve["낙찰확률"].isna().all() or curve["낙찰확률"].max() <= 0:
        return []
    order = curve["낙찰확률"].sort_values(ascending=False).index
    picked = []
    for i in order:
        s = curve.at[i, "사정율"]
        if all(abs(s - curve.at[j, "사정율"]) >= min_gap for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    return [{"사정율": float(curve.at[i, "사정율"]), "낙찰확률": float(curve.at[i, "낙찰확률"]),
             "하한통과확률": float(curve.at[i, "하한통과확률"])} for i in picked]


def sajung_table(hist: pd.DataFrame, base_amt: float, lower_rate: float, a_value: float = 0,
                 lo: float = 98.0, hi: float = 102.0, step: float = 0.05) -> pd.DataFrame:
    """사정율 후보별 투찰금액과 과거 낙찰확률."""
    c = win_curve(hist, lo, hi, 0.01)
    c = c[np.isclose((c["사정율"] / step).round() * step, c["사정율"])].reset_index(drop=True)
    return pd.DataFrame({
        "사정율(%)": c["사정율"],
        "투찰금액(원)": [bid_amount(base_amt, s, lower_rate, a_value) for s in c["사정율"]],
        "낙찰확률(%)": c["낙찰확률"],
        "하한 통과확률(%)": c["하한통과확률"],
    })


def recommend(hist: pd.DataFrame, top: int = 3) -> list[dict]:
    """과거 데이터 기준 낙찰확률이 가장 높은 사정율."""
    w = win_windows(hist)
    if len(w) < 5:
        return []
    out = []
    for p in best_points(win_curve(hist), top):
        out.append({"추천 사정율": round(p["사정율"], 3), "낙찰확률(%)": round(p["낙찰확률"], 1),
                    "하한 통과확률(%)": round(p["하한통과확률"], 1), "비교 공고 수": len(w)})
    return out


def drawn_number_stats(bid_keys) -> pd.DataFrame:
    """예비가격 번호(1~15)별 추첨 빈도."""
    df = _prelim(bid_keys)
    if df.empty:
        return pd.DataFrame(columns=["번호", "추첨횟수"])
    return (df.groupby("sno").agg(추첨횟수=("drawn", "sum"), 전체=("drawn", "size"))
            .reset_index().rename(columns={"sno": "번호"}))


def _prelim(bid_keys) -> pd.DataFrame:
    keys = list(bid_keys)
    if not keys:
        return pd.DataFrame(columns=["bid_key", "sno", "price", "drawn"])
    out = []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        ph = ",".join(f":k{j}" for j in range(len(chunk)))
        out.append(db.query_df(f"SELECT bid_key, sno, price, drawn FROM prelim_prices "
                               f"WHERE bid_key IN ({ph})", {f"k{j}": k for j, k in enumerate(chunk)}))
    return pd.concat(out, ignore_index=True)


def prelim_position_stats(bid_keys) -> pd.DataFrame:
    """예비가격 15개를 기초금액 대비 %로 바꿨을 때, 추첨된 가격들이 어느 위치에 있었나."""
    df = _prelim(bid_keys)
    if df.empty:
        return df
    plans = db.query_df("SELECT bid_key, base_amt FROM plans")
    df = df.merge(plans, on="bid_key", how="left")
    df["pct"] = df["price"] / df["base_amt"] * 100
    return df


def load_ranks(bid_keys=None) -> pd.DataFrame:
    """개찰순위(업체별 투찰) + 각 업체가 노린 사정율."""
    df = db.query_df(
        """SELECT k.bid_key, k.rank, k.bizno, k.company, k.amount, k.rate, k.note,
                  b.name, b.org, b.open_dt, b.lower_rate,
                  COALESCE(p.base_amt, ba.base_amt) AS base_amt, ba.a_value, p.plan_price
           FROM ranks k JOIN bids b ON b.bid_key = k.bid_key
           LEFT JOIN base_amounts ba ON ba.bid_key = k.bid_key
           LEFT JOIN plans p ON p.bid_key = k.bid_key
           WHERE k.rank > 0""")
    if df.empty:
        return df
    if bid_keys is not None:
        df = df[df["bid_key"].isin(set(bid_keys))]
    a = df["a_value"].fillna(0)
    r = df["lower_rate"] / 100
    # 업체가 노린 사정율 = 투찰금액을 하한율로 역산한 예정가격 / 기초금액
    df["투찰사정율"] = ((df["amount"] - a) / r + a) / df["base_amt"] * 100
    df.loc[(df["투찰사정율"] < 90) | (df["투찰사정율"] > 110), "투찰사정율"] = np.nan
    df["open_date"] = pd.to_datetime(df["open_dt"], errors="coerce", format="mixed")
    return df


def competitor_stats(ranks: pd.DataFrame, min_count: int = 2) -> pd.DataFrame:
    """업체별 투찰 경향."""
    if ranks.empty:
        return pd.DataFrame()
    key = ranks["bizno"].where(ranks["bizno"].fillna("") != "", ranks["company"])
    agg = ranks.assign(_k=key).groupby("_k").agg(**{
        "업체명": ("company", "last"),
        "사업자번호": ("bizno", "last"),
        "참가": ("bid_key", "nunique"),
        "1순위": ("rank", lambda x: int((x == 1).sum())),
        "10위내": ("rank", lambda x: int((x <= 10).sum())),
        "평균 투찰사정율": ("투찰사정율", "mean"),
        "사정율 편차": ("투찰사정율", "std"),
        "최근 참가": ("open_date", "max"),
    }).reset_index(drop=True)
    agg["최근 참가"] = agg["최근 참가"].dt.strftime("%Y-%m-%d")
    agg = agg[agg["참가"] >= min_count]
    return agg.sort_values(["1순위", "10위내", "참가"], ascending=False).round(3)


def default_keywords():
    return load_settings()["target_keywords"]
