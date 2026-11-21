"""분석 로직: 사정율, 1순위 사정율, 투찰금액 계산."""
import math
import re

import numpy as np
import pandas as pd

import db
from config import load_settings


def load_dataset() -> pd.DataFrame:
    """공고 + 기초금액 + 예정가격 + 개찰결과 + 낙찰자를 공고 단위로 합친 표."""
    df = db.query_df(
        """
        SELECT b.bid_key, b.bid_no, b.bid_ord, b.name, b.org, b.demand_org, b.region,
               b.main_type, b.method, b.contract_method, b.notice_dt, b.close_dt, b.open_dt, b.est_price,
               b.lower_rate, b.url,
               ba.base_amt AS base_amt_notice, ba.a_value, ba.range_begin, ba.range_end,
               ba.a_apply, ba.pure_cost, ba.a_detail,
               p.plan_price, p.base_amt AS base_amt_open,
               o.participants AS participants_open, o.status, o.first_company, o.first_amt, o.first_bizno,
               r.participants AS participants_final, r.winner, r.win_amt, r.win_rate, r.winner_bizno
        FROM bids b
        LEFT JOIN base_amounts ba ON ba.bid_key = b.bid_key
        LEFT JOIN plans p ON p.bid_key = b.bid_key
        LEFT JOIN openings o ON o.bid_key = b.bid_key
        LEFT JOIN results r ON r.bid_key = b.bid_key
        """
    )
    if df.empty:
        return df
    # 참가가능지역·면허제한 (공고별로 여러 개 → 쉼표로 묶음)
    for table, col in (("regions", "region"), ("licenses", "license")):
        t = db.query_df(f"SELECT bid_key, {col} FROM {table}")
        joined = t.groupby("bid_key")[col].agg(lambda x: ", ".join(sorted(set(x)))) if len(t) else pd.Series(dtype=str)
        out = "allowed_regions" if table == "regions" else "licenses"
        df[out] = df["bid_key"].map(joined).fillna("")
        # 그 공고 등록일의 데이터를 받았으면 '확인됨' (빈 값 = 제한 없음)
        days = db.done_days(table)
        df[out + "_known"] = df["notice_dt"].astype(str).str[:10].isin(days) | (df[out] != "")
    df["base_amt"] = df["base_amt_open"].fillna(df["base_amt_notice"])
    df["a_value"] = df["a_value"].fillna(0)
    df["participants"] = df["participants_open"].where(df["participants_open"] > 0,
                                                       df["participants_final"])
    # 1순위 = 개찰 1순위(개찰 직후 공개), 없으면 최종 낙찰자
    df["first_company"] = df["first_company"].fillna(df["winner"])
    df["first_amt"] = df["first_amt"].fillna(df["win_amt"])
    # 낙찰 기준 금액: 적격심사에서 1순위가 탈락하면 2·3순위가 낙찰되므로, 최종 낙찰자 금액이 있으면 그것을 사용
    # (최종 낙찰자가 1순위보다 낮을 수는 없으므로 그런 데이터는 무시)
    final_ok = df["win_amt"].notna() & (df["first_amt"].isna() | (df["win_amt"] >= df["first_amt"] * 0.9999))
    df["win_ref_amt"] = df["win_amt"].where(final_ok, df["first_amt"])
    fb = df["first_bizno"].fillna("").astype(str).str.replace("-", "")
    wb = df["winner_bizno"].fillna("").astype(str).str.replace("-", "")
    df["first_won"] = np.where((fb != "") & (wb != ""), (fb == wb).astype(float), np.nan)
    # 사정율(%) = 예정가격 / 기초금액
    df["sajung"] = df["plan_price"] / df["base_amt"] * 100
    # 낙찰자 사정율: 낙찰자가 '노린' 사정율 = 낙찰금액을 하한율로 역산한 예정가격 / 기초금액
    r = df["lower_rate"] / 100
    implied_plan = (df["win_ref_amt"] - df["a_value"]) / r + df["a_value"]
    df["winner_sajung"] = implied_plan / df["base_amt"] * 100
    implied_first = (df["first_amt"] - df["a_value"]) / r + df["a_value"]
    df["first_sajung"] = implied_first / df["base_amt"] * 100
    df["open_date"] = pd.to_datetime(df["open_dt"], errors="coerce", format="mixed")
    # 이상치 제거 (데이터 오류 방지)
    df.loc[(df["sajung"] < 90) | (df["sajung"] > 110), "sajung"] = np.nan
    df.loc[(df["winner_sajung"] < 90) | (df["winner_sajung"] > 110), "winner_sajung"] = np.nan
    df.loc[(df["first_sajung"] < 90) | (df["first_sajung"] > 110), "first_sajung"] = np.nan
    return df


def eligible(df: pd.DataFrame, company_region: str | None = None,
             company_licenses: list[str] | None = None) -> pd.Series:
    """우리 회사가 참가 가능한 공고인지.

    - 지역: 참가가능지역이 없으면(전국) 가능, 있으면 우리 지역이 포함돼야 가능.
      아직 지역 정보를 못 받은 공고는 공사지역·기관명에 우리 지역이 있으면 가능으로 봄.
    - 면허: 면허제한이 확인되고 값이 있으면 우리 면허가 포함돼야 가능.
    """
    c = db.company()
    reg = company_region if company_region is not None else c["region"]
    lic = company_licenses if company_licenses is not None else c["licenses"]
    ok = pd.Series(True, index=df.index)
    if reg and "allowed_regions" in df:
        known = df["allowed_regions_known"]
        allowed = df["allowed_regions"]
        place = df["region"].fillna("") + " " + df["org"].fillna("") + " " + df["demand_org"].fillna("")
        ok &= (known & ((allowed == "") | allowed.str.contains(reg, regex=False))) | \
              (~known & place.str.contains(reg, regex=False))
    if lic and "licenses" in df:
        has = df["licenses"] != ""
        ok &= ~has | df["licenses"].str.contains("|".join(map(re.escape, lic)), regex=True)
    return ok


def apply_filters(df: pd.DataFrame, keywords=None, regions=None, orgs=None,
                  min_amt=None, max_amt=None, date_from=None, date_to=None,
                  name_query=None, eligible_only: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if eligible_only:
        m &= eligible(df)
    if keywords:
        text = df["name"].fillna("") + " " + df["main_type"].fillna("")
        if "licenses" in df:
            text = text + " " + df["licenses"]
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
    # 한 공고의 낙찰 구간은 매우 좁아서 그래프가 들쭉날쭉함 → 범위 평균으로 평활화
    # 표본이 적을수록 더 넓게 평균 (우연히 한 번 맞은 지점에 끌려가지 않도록)
    n = len(w)
    width = 0.05 if n >= 200 else 0.1 if n >= 50 else 0.2
    k = max(1, int(round(width / step)))
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


def similar_history(df: pd.DataFrame, row, keywords=None) -> pd.DataFrame:
    """추천 계산용 과거 공고: 같은 업종, 기초금액 ±50%, 같은 기관 데이터가 충분하면 기관 우선."""
    h = apply_filters(df[df["sajung"].notna()], keywords=keywords)
    base = row["base_amt"]
    if pd.notna(base) and base > 0:
        h = h[(h["base_amt"] >= base * 0.5) & (h["base_amt"] <= base * 1.5)]
    same = h[h["org"] == row["org"]]
    return same if len(win_windows(same)) >= 15 else h


def evidence(hist: pd.DataFrame, s_: float, n_examples: int = 3) -> dict:
    """선택한 사정율 s_ 의 근거: 과거 공고에 그대로 넣었다면 결과가 어땠는지."""
    w = hist.dropna(subset=["sajung", "winner_sajung"])
    w = w[w["winner_sajung"] >= w["sajung"]]
    n = len(w)
    win = (w["sajung"] <= s_) & (s_ < w["winner_sajung"])
    low = s_ < w["sajung"]
    p = w["participants"].dropna()
    p = p[p > 0]
    expected = float((1 / p).sum() * n / len(p)) if len(p) else None   # 무작위로 넣었을 때 기대 1순위 건수
    sj = hist["sajung"].dropna()
    ex = w.sort_values("open_date", ascending=False).head(n_examples)
    examples = []
    for _, r in ex.iterrows():
        if s_ < r["sajung"]:
            res = "하한 미달"
        elif s_ < r["winner_sajung"]:
            res = "🏆 낙찰"
        else:
            res = "낙찰자보다 높음"
        examples.append({"공고명": r["name"], "개찰일": r["open_date"].strftime("%m/%d") if pd.notna(r["open_date"]) else "-",
                         "실제": r["sajung"], "1순위": r["winner_sajung"], "결과": res})
    return {
        "n": n, "wins": int(win.sum()), "low": int(low.sum()),
        "expected": expected,
        "q25": float(sj.quantile(0.25)) if len(sj) else None,
        "median": float(sj.median()) if len(sj) else None,
        "q75": float(sj.quantile(0.75)) if len(sj) else None,
        "examples": examples,
    }


PRIOR_WEIGHT = 100  # 상위 단계 데이터를 '가상의 100건'만큼 섞음 (하위 단계 데이터가 많을수록 하위 비중↑)


def _band(h: pd.DataFrame, base, min_n: int) -> pd.DataFrame:
    """기초금액 ±50% 공고가 충분하면 그것만, 아니면 전체."""
    if pd.notna(base) and base > 0:
        b = h[(h["base_amt"] >= base * 0.5) & (h["base_amt"] <= base * 1.5)]
        if len(win_windows(b)) >= min_n:
            return b
    return h


def is_local_bid(row, region: str | None = None) -> bool:
    """우리 지역(울산) 지역제한 공고인지."""
    reg = region if region is not None else db.company()["region"]
    if not reg:
        return False
    allowed = str(row.get("allowed_regions", "") or "")
    if allowed:
        return reg in allowed
    if row.get("allowed_regions_known", False):
        return False  # 지역제한 없음(전국 공고)
    place = f"{row.get('region', '')} {row.get('org', '')} {row.get('demand_org', '')}"
    return reg in place


def local_region_history(done: pd.DataFrame, region: str) -> pd.DataFrame:
    """우리 지역 지역제한 공고 (지역 정보를 아직 못 받은 공고는 공사지역·기관명으로 판단)."""
    allowed = done["allowed_regions"] if "allowed_regions" in done else pd.Series("", index=done.index)
    known = done["allowed_regions_known"] if "allowed_regions_known" in done else pd.Series(False, index=done.index)
    place = done["region"].fillna("") + " " + done["org"].fillna("") + " " + done["demand_org"].fillna("")
    m = allowed.str.contains(region, regex=False) | (~known & (allowed == "") & place.str.contains(region, regex=False))
    return done[m]


def tiers(df: pd.DataFrame, row, keywords=None) -> list[tuple[str, pd.DataFrame]]:
    """비교 단계: [전국, (우리 지역), (같은 기관)] — 넓은 것부터."""
    done = df[df["sajung"].notna()]
    region = db.company()["region"]
    ind = apply_filters(done, keywords=keywords) if keywords else done
    if len(win_windows(ind)) < 30:
        ind = done
    out = [("전국 같은 업종", _band(ind, row["base_amt"], 200))]
    if region and is_local_bid(row, region):
        loc = local_region_history(ind, region)
        if len(win_windows(loc)):
            out.append((f"{region} 지역제한 공고", _band(loc, row["base_amt"], 100)))
    org = ind[ind["org"] == row["org"]]
    if len(win_windows(org)) >= 5:
        out.append((f"같은 기관({row['org']})", org))
    return out


def blend(tier_list) -> tuple[pd.DataFrame, list[float]]:
    """넓은 단계부터 차례로 섞기: p = (n·p_하위 + 100·p_상위) / (n + 100). 각 단계 반영 비중도 반환."""
    curve = win_curve(tier_list[0][1])
    p = curve["낙찰확률"].fillna(0)
    weights = [1.0]
    for _, h in tier_list[1:]:
        n = len(win_windows(h))
        c = win_curve(h)["낙찰확률"].fillna(0)
        w = n / (n + PRIOR_WEIGHT)
        p = w * c + (1 - w) * p
        weights = [x * (1 - w) for x in weights] + [w]
    return curve.assign(낙찰확률=p.round(3)), weights


def qual_check(row) -> tuple[bool | None, str]:
    """적격심사 자가진단 (회사가 등록한 '통과 가능한 최대 추정가격' 기준의 간이 판정)."""
    lim = db.get_kv("qual_max_est")
    try:
        lim = float(lim) * 1e8 if lim not in (None, "") else None
    except ValueError:
        lim = None
    est = row.get("est_price")
    if lim is None or est is None or pd.isna(est):
        return None, ""
    if est <= lim:
        return True, "적격심사 통과 가능 금액대"
    return False, f"추정가격 {est / 1e8:,.1f}억 > 우리 통과 가능 {lim / 1e8:,.1f}억 → 1순위여도 심사 탈락 위험"


PURE_COST_RATE = 0.98  # 적격심사: 투찰금액이 순공사원가의 98% 미만이면 낙찰 제외


def pure_cost_min(row) -> int | None:
    pc = row.get("pure_cost")
    return int(math.ceil(pc * PURE_COST_RATE)) if pc is not None and pd.notna(pc) and pc > 0 else None


def min_sajung_for_amount(base, lr, a_val, amount) -> float:
    """투찰금액이 amount 이상이 되는 최소 사정율(0.01% 단위 올림)."""
    s_ = ((amount - a_val) / (lr / 100) + a_val) / base * 100
    s_ = math.ceil(s_ * 100) / 100
    while bid_amount(base, s_, lr, a_val) < amount:
        s_ = round(s_ + 0.01, 2)
    return s_


def pick_bid(df: pd.DataFrame, row, keywords=None, use_region: bool = True) -> dict | None:
    """공고 하나에 대한 최종 투찰금액 1개와 근거.

    단계별로 섞어서 낙찰확률 계산: 전국 같은 업종 → (우리 지역 지역제한 공고) → (같은 기관)
    하위 단계 데이터가 많을수록 그 단계 성향을 더 따르고, 적으면 상위 단계를 따른다.
    울산 지역제한 공고에는 울산 업체만 들어오므로 울산 단계가 중요하고,
    전국 공고(지역제한 없음)에는 울산 단계를 쓰지 않는다.
    """
    base, lr = row["base_amt"], row["lower_rate"]
    if pd.isna(base) or pd.isna(lr) or base <= 0:
        return None
    a_val = row["a_value"] or 0
    done = df[df["sajung"].notna()]
    tl = tiers(df, row, keywords)
    if not use_region:
        tl = [t for t in tl if "지역제한" not in t[0]]
    if len(win_windows(tl[0][1])) < 5:
        s_ = float(done["sajung"].median()) if len(done) else 100.0
        return {"사정율": round(s_, 2), "투찰금액": bid_amount(base, s_, lr, a_val), "신뢰도": "낮음",
                "근거": "과거 사정율 중앙값(데이터 부족)", "낙찰확률": None, "무작위 대비": None, "후보": [],
                "단계": [], "근거상세": evidence(done, round(s_, 2))}
    curve, weights = blend(tl)
    floor_amt = pure_cost_min(row)
    floor_s = min_sajung_for_amount(base, lr, a_val, floor_amt) if floor_amt else None
    if floor_s is not None:
        # 순공사원가 98% 미만 사정율은 후보에서 제외
        curve = curve.assign(낙찰확률=curve["낙찰확률"].where(curve["사정율"] >= floor_s - 1e-9, 0))
    picks = best_points(curve, top=3)
    if not picks:
        picks = [{"사정율": floor_s or 100.0, "낙찰확률": 0.0, "하한통과확률": 0.0}]
    s_ = round(picks[0]["사정율"], 2)
    if floor_s is not None and s_ < floor_s:
        s_ = floor_s
    steps = [{"이름": name, "비중": w, **evidence(h, s_)} for (name, h), w in zip(tl, weights)]
    nat = steps[0]
    closest = tl[-1][1]
    bp = baseline_prob(closest if len(closest) >= 10 else tl[0][1])
    times = picks[0]["낙찰확률"] / bp if bp else None
    n_close = sum(st_["n"] for st_ in steps[1:])
    if n_close >= 30 and nat["wins"] >= 5 and (times or 0) >= 2:
        conf = "높음"
    elif nat["wins"] >= 3 and (times or 0) >= 1.5:
        conf = "보통"
    else:
        conf = "낮음"
    near = closest if len(closest) >= 10 else tl[0][1]
    pp = near["participants"].dropna()
    pp = pp[pp > 0]
    fw = near["first_won"].dropna() if "first_won" in near else pd.Series(dtype=float)
    return {
        "예상 참가": int(pp.median()) if len(pp) else None,
        "1순위 낙찰률": float(fw.mean() * 100) if len(fw) >= 10 else None,
        "적격심사": qual_check(row),
        "사정율": s_, "투찰금액": bid_amount(base, s_, lr, a_val), "신뢰도": conf,
        "근거": steps[-1]["이름"], "낙찰확률": round(picks[0]["낙찰확률"], 2), "무작위 대비": times,
        "후보": [(round(x["사정율"], 2), bid_amount(base, round(x["사정율"], 2), lr, a_val), round(x["낙찰확률"], 2))
               for x in picks],
        "단계": steps, "근거상세": steps[-1],
        "순공사원가 하한": floor_amt, "순공사원가 사정율": floor_s,
    }


def evidence_lines(pk: dict) -> list[str]:
    """근거를 사람이 읽는 문장으로 (화면·알림 공용, HTML 없음)."""
    out = []
    steps = pk.get("단계") or []
    for st_ in reversed(steps):  # 가까운 단계부터
        out.append(f"{st_['이름']} 과거 {st_['n']}건: 이 사정율이면 {st_['wins']}건 낙찰, {st_['low']}건 하한 미달")
    if len(steps) > 1:
        out.append("반영 비중: " + " · ".join(f"{x['이름'].split('(')[0]} {x['비중'] * 100:.0f}%" for x in reversed(steps)))
    elif not steps:
        e = pk["근거상세"]
        out.append(f"비교 대상: 과거 공고 {e['n']}건 (데이터 부족)")
    if pk.get("무작위 대비"):
        out.append(f"과거 낙찰확률 {pk['낙찰확률']}% = 아무렇게나 넣을 때의 {pk['무작위 대비']:.1f}배")
    if pk.get("1순위 낙찰률") is not None:
        out.append(f"비슷한 공고에서 개찰 1순위가 그대로 낙찰된 비율 {pk['1순위 낙찰률']:.0f}% "
                   "(나머지는 1순위 심사 탈락 → 다음 순위 낙찰, 계산에 반영됨)")
    if pk.get("순공사원가 하한"):
        out.append(f"순공사원가 98% 기준: {pk['순공사원가 하한']:,}원 이상 (사정율 {pk['순공사원가 사정율']:.2f}% 이상) → 반영됨")
    e = pk["근거상세"]
    if e.get("median") is not None:
        out.append(f"{e.get('이름', '비교 공고')} 실제 사정율: 보통 {e['q25']:.2f} ~ {e['q75']:.2f}% "
                   f"(중앙 {e['median']:.2f}%)")
    return out


def baseline_prob(hist: pd.DataFrame) -> float | None:
    """무작위로 넣었을 때 기대 낙찰확률 = 평균(1/참가업체수)."""
    p = hist["participants"].dropna()
    p = p[p > 0]
    return float((1 / p).mean() * 100) if len(p) else None


def my_bids_view(df: pd.DataFrame) -> pd.DataFrame:
    """우리 투찰 기록 + 공고 결과 + 판정."""
    mb = db.query_df("SELECT * FROM my_bids")
    if mb.empty:
        return mb
    v = mb.merge(df, on="bid_key", how="left", suffixes=("", "_bid"))
    a = v["a_value"].fillna(0)
    r = v["lower_rate"] / 100
    # 금액만 적었으면 사정율 역산, 사정율만 적었으면 금액 계산
    calc_s = ((v["amount"] - a) / r + a) / v["base_amt"] * 100
    v["sajung"] = v["sajung"].fillna(calc_s)
    calc_amt = np.ceil((v["base_amt"] * v["sajung"] / 100 - a) * r + a)
    v["amount"] = v["amount"].fillna(calc_amt)
    floor = (v["plan_price"] - a) * r + a

    ours = db.company()["bizno"]

    def judge(x, fl):
        if pd.isna(x["plan_price"]) and pd.isna(x["first_amt"]):
            return "개찰 전" if pd.isna(x["open_date"]) or x["open_date"] > pd.Timestamp.now() else "결과 대기"
        if pd.notna(fl) and pd.notna(x["amount"]) and x["amount"] < fl:
            return "하한 미달"
        wb = str(x.get("winner_bizno") or "").replace("-", "")
        if ours and wb:
            if wb == ours:
                return "🏆 낙찰"
            if pd.notna(x["my_rank"]) and x["my_rank"] == 1:
                return "1순위였으나 낙찰 실패(심사 탈락 추정)"
        if pd.notna(x["my_rank"]) and x["my_rank"] == 1:
            return "🏆 1순위"
        if pd.notna(x["first_amt"]) and pd.notna(x["amount"]) and x["amount"] <= x["first_amt"]:
            return "🏆 1순위"
        if pd.notna(x["my_rank"]) and x["my_rank"] > 0:
            return f"{int(x['my_rank'])}위"
        if pd.notna(x["first_amt"]) and pd.notna(x["amount"]) and x["amount"] > x["first_amt"]:
            return "1순위보다 높음"
        return "순위 확인 중"
    v["결과"] = [judge(x, fl) for (_, x), fl in zip(v.iterrows(), floor)]
    # 낙찰 구간 대비 우리 위치: + 면 너무 높게(1순위보다 비쌈), - 면 너무 낮게(하한 미달)
    v["낙찰구간"] = [
        "-" if pd.isna(lo) or pd.isna(hi) else f"{lo:.3f} ~ {hi:.3f}"
        for lo, hi in zip(v["sajung_bid"] if "sajung_bid" in v else v["sajung"], v["winner_sajung"])]
    return v


# 면허 → 공고 검색 키워드 (공고명·주공종·면허제한 텍스트에서 찾음)
INDUSTRY_KEYWORDS = {
    "전기공사업": ["전기"],
    "정보통신공사업": ["통신"],
    "소방시설공사업": ["소방"],
    "전문소방시설공사업": ["소방"],
    "일반소방시설공사업": ["소방"],
    "기계설비": ["기계설비", "설비"],
    "실내건축공사업": ["실내건축"],
    "토목건축공사업": ["토목", "건축"],
    "토목공사업": ["토목"],
    "건축공사업": ["건축"],
    "조경공사업": ["조경"],
}


def keywords_for_license(lic: str) -> list[str]:
    for k, v in INDUSTRY_KEYWORDS.items():
        if k in lic or lic in k:
            return v
    return [lic.replace("공사업", "").strip() or lic]


def company_keywords(licenses: list[str] | None = None) -> list[str]:
    """회사 보유 면허 기준 업종 키워드 (없으면 설정의 기본 키워드)."""
    lic = licenses if licenses is not None else db.company()["licenses"]
    out = []
    for x in lic:
        for k in keywords_for_license(x):
            if k not in out:
                out.append(k)
    return out or load_settings()["target_keywords"]


def bid_keywords(row, licenses: list[str] | None = None) -> list[str]:
    """이 공고가 우리 면허 중 어느 업종인지 → 그 업종 키워드 (추천 계산 시 같은 업종끼리 비교)."""
    lic = licenses if licenses is not None else db.company()["licenses"]
    text = f"{row.get('licenses', '')} {row.get('name', '')} {row.get('main_type', '')}"
    for x in lic:
        if x in text:
            return keywords_for_license(x)
    for x in lic:
        kws = keywords_for_license(x)
        if any(k in text for k in kws):
            return kws
    return company_keywords(lic)


def default_keywords():
    return company_keywords()


# ---------------- 추천 방식 검증 (백테스트) ----------------

def _pick_from_curve(curve: pd.DataFrame) -> float | None:
    pts = best_points(curve, top=1)
    return round(pts[0]["사정율"], 2) if pts else None


def strategy_picks(hist: pd.DataFrame, row, keywords) -> dict:
    """같은 과거 데이터로 여러 방식의 사정율 선택."""
    done = hist[hist["sajung"].notna()]
    tl = tiers(hist, row, keywords)
    out = {}
    if len(win_windows(tl[0][1])) < 5:
        return out
    out["① 현재 방식 (기관+울산+전국)"] = _pick_from_curve(blend(tl)[0])
    no_reg = [t for t in tl if "지역제한" not in t[0]]
    if len(no_reg) != len(tl):
        out["② 울산 단계 없이 (기관+전국)"] = _pick_from_curve(blend(no_reg)[0])
    out["③ 전국 같은 업종만"] = _pick_from_curve(win_curve(tl[0][1]))
    reg = [t for t in tl if "지역제한" in t[0]]
    if reg and len(win_windows(reg[0][1])) >= 5:
        out["④ 울산 지역제한 공고만"] = _pick_from_curve(win_curve(reg[0][1]))
    out["⑤ 단순 평균 (사정율 평균)"] = round(float(tl[0][1]["sajung"].mean()), 2)
    w = tl[0][1]["winner_sajung"].dropna()
    if len(w):
        out["⑥ 과거 1순위 사정율 평균"] = round(float(w.mean()), 2)
    return out


def backtest(df: pd.DataFrame, keywords, test_days: int = 90, max_tests: int = 300,
             min_history: int = 50, progress=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """최근 test_days 일 동안의 공고 각각에 대해, 그 공고 '이전' 데이터만으로 사정율을 골랐다면
    실제로 1순위였는지 확인. (미래 데이터를 쓰지 않는 공정한 시험)"""
    done = df[df["sajung"].notna() & df["winner_sajung"].notna() & df["open_date"].notna()]
    done = done[done["winner_sajung"] >= done["sajung"]].sort_values("open_date")
    tests = apply_filters(done, keywords=keywords)
    tests = tests[tests["open_date"] >= pd.Timestamp.now() - pd.Timedelta(days=test_days)]
    if len(tests) > max_tests:
        tests = tests.sample(max_tests, random_state=0).sort_values("open_date")
    # 개찰순위(상위 업체 투찰금액)가 있으면 '가상 순위'까지 계산
    rk = db.query_df("SELECT bid_key, amount FROM ranks WHERE rank > 0 AND amount IS NOT NULL")
    amounts = {k: np.sort(g["amount"].values) for k, g in rk.groupby("bid_key")} if len(rk) else {}
    rows = []
    for i, (_, r) in enumerate(tests.iterrows()):
        if progress:
            progress(i + 1, len(tests))
        hist = df[df["open_date"] < r["open_date"]]
        if hist["sajung"].notna().sum() < min_history:
            continue
        picks = strategy_picks(hist, r, bid_keywords(r))
        p = r["participants"]
        for name, s_ in picks.items():
            if s_ is None:
                continue
            res = "1순위" if r["sajung"] <= s_ < r["winner_sajung"] else ("하한 미달" if s_ < r["sajung"] else "1순위보다 높음")
            vrank = np.nan
            comp = amounts.get(r["bid_key"])
            if comp is not None and len(comp) and res != "하한 미달" and pd.notna(r["lower_rate"]):
                mine = bid_amount(r["base_amt"], s_, r["lower_rate"], r["a_value"] or 0)
                vrank = 1 + int((comp < mine).sum())
                if vrank > len(comp):
                    vrank = np.inf  # 받은 상위권 밖
            rows.append({"방식": name, "bid_key": r["bid_key"], "공고명": r["name"], "개찰일": r["open_date"],
                         "추천 사정율": s_, "실제 사정율": r["sajung"], "1순위 사정율": r["winner_sajung"],
                         "결과": res, "가상 순위": vrank, "순위 있음": comp is not None,
                         "무작위 기대": (1 / p) if pd.notna(p) and p > 0 else np.nan})
    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail, detail
    g = detail.groupby("방식")
    summary = pd.DataFrame({
        "시험 공고": g.size(),
        "1순위": g["결과"].apply(lambda x: int((x == "1순위").sum())),
        "하한 미달": g["결과"].apply(lambda x: int((x == "하한 미달").sum())),
        "무작위 기대 1순위": g["무작위 기대"].sum().round(2),
    })
    summary["낙찰률(%)"] = (summary["1순위"] / summary["시험 공고"] * 100).round(2)
    summary["무작위 대비(배)"] = (summary["1순위"] / summary["무작위 기대 1순위"]).round(2)
    summary["하한 미달률(%)"] = (summary["하한 미달"] / summary["시험 공고"] * 100).round(1)
    ranked = detail[detail["순위 있음"]]
    if len(ranked):
        gr = ranked.groupby("방식")
        summary["순위 비교 공고"] = gr.size()
        summary["10위 이내"] = gr["가상 순위"].apply(lambda x: int((x <= 10).sum()))
        summary["10위 이내율(%)"] = (summary["10위 이내"] / summary["순위 비교 공고"] * 100).round(1)
        summary["중앙 순위(유효 투찰)"] = gr["가상 순위"].apply(
            lambda x: x[np.isfinite(x)].median() if np.isfinite(x).any() else np.nan)
    sort_cols = ["10위 이내", "1순위"] if "10위 이내" in summary else ["1순위", "무작위 대비(배)"]
    summary = summary.reset_index().sort_values(sort_cols, ascending=False)
    return summary, detail
