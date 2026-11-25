"""분석 로직: 사정율, 1순위 사정율, 투찰금액 계산."""
import math
import re

import numpy as np
import pandas as pd

import db
from config import load_settings


_BASE_SQL = """
        SELECT b.bid_key, b.bid_no, b.bid_ord, b.name, b.org, b.demand_org, b.region,
               b.main_type, b.method, b.contract_method, b.notice_dt, b.close_dt, b.open_dt, b.est_price,
               b.lower_rate, b.url, b.spec_docs, b.ntce_kind, b.qual_dt, b.begin_dt,
               b.perf_limit, b.pq, b.designated, b.ability_list, b.joint_duty, b.detail_bid, b.joint_rate,
               ba.base_amt AS base_amt_notice, ba.a_value, ba.range_begin, ba.range_end,
               ba.a_apply, ba.pure_cost, ba.a_detail,
               p.plan_price, p.base_amt AS base_amt_open,
               o.participants AS participants_open, o.status, o.first_company, o.first_amt, o.first_bizno,
               o.first_rate,
               r.participants AS participants_final, r.winner, r.win_amt, r.win_rate, r.winner_bizno
        FROM bids b
        LEFT JOIN base_amounts ba ON ba.bid_key = b.bid_key
        LEFT JOIN plans p ON p.bid_key = b.bid_key
        LEFT JOIN openings o ON o.bid_key = b.bid_key
        LEFT JOIN results r ON r.bid_key = b.bid_key
"""
# 최근 공고만 다시 받는 조건 (개찰 결과·낙찰자는 개찰 후 2주 안에 채워짐)
# 국방(D2B…)은 과거 결과를 매일 조금씩 채우므로 항상 새로 받음 (건수가 적어 부담 없음)
_RECENT_WHERE = "WHERE b.open_dt IS NULL OR b.open_dt >= :cut OR b.notice_dt >= :cut OR b.bid_key LIKE 'D2B%'"
CACHE_FULL_DAYS = 7      # 이 주기로 전체를 새로 받음 (삭제·정정 반영)
CACHE_MARGIN_DAYS = 14   # 마지막으로 받은 날보다 이만큼 앞부터 다시 받음
_mem: dict = {}


def _cache_path() -> str:
    import hashlib
    import os
    d = os.environ.get("BID_CACHE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ds_" + hashlib.md5(db.url().encode()).hexdigest()[:10] + ".pkl")


def _raw_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(공고 합친 표, 참가지역, 면허제한) — 원격 DB 전송량을 줄이려고 파일에 보관하고 최근 공고만 새로 받음."""
    import os
    import pickle
    path = _cache_path()
    now = pd.Timestamp.now()
    cache = None
    if db.is_remote() and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                cache = pickle.load(f)
            mark = db.get_kv("dataset_full_reload_after")
            if (now - cache["full_at"]).days >= CACHE_FULL_DAYS or cache.get("sql") != _BASE_SQL or \
                    (mark and pd.Timestamp(mark) > cache["full_at"]):
                cache = None   # 오래됐거나, 항목이 바뀌었거나, 과거 자료를 새로 채웠으면 전체 다시
        except Exception:  # noqa: BLE001  손상된 파일은 무시하고 전체 다시 받기
            cache = None
    # 자동 수집(GitHub Actions) 안에서는 방금(10분 안) 받은 표를 그대로 씀: 한 실행 안에서 여러 단계·알림이
    # 같은 표를 매번 다시 받으면 무료 전송량(월 5GB)을 금방 넘김. 사이트는 항상 최신으로 받음.
    if cache is not None and os.environ.get("GITHUB_ACTIONS") and (now - cache["at"]).total_seconds() < 600:
        return cache["base"], cache["regions"], cache["licenses"]
    if cache is None:
        base = db.query_df_big(_BASE_SQL)
        regs = db.query_df_big("SELECT bid_key, region FROM regions")
        lics = db.query_df_big("SELECT bid_key, license FROM licenses")
        cache = {"full_at": now, "at": now, "base": base, "regions": regs, "licenses": lics, "sql": _BASE_SQL}
    else:
        cut = (cache["at"] - pd.Timedelta(days=CACHE_MARGIN_DAYS)).strftime("%Y-%m-%d")
        p = {"cut": cut}
        base_new = db.query_df(_BASE_SQL + _RECENT_WHERE, p)
        sub = f"SELECT b.bid_key FROM bids b {_RECENT_WHERE}"
        regs_new = db.query_df(f"SELECT bid_key, region FROM regions WHERE bid_key IN ({sub})", p)
        lics_new = db.query_df(f"SELECT bid_key, license FROM licenses WHERE bid_key IN ({sub})", p)
        keys = set(base_new["bid_key"])
        cache["base"] = pd.concat([cache["base"][~cache["base"]["bid_key"].isin(keys)], base_new], ignore_index=True)
        for name, new in (("regions", regs_new), ("licenses", lics_new)):
            old = cache[name]
            cache[name] = pd.concat([old[~old["bid_key"].isin(keys)], new], ignore_index=True)
        cache["at"] = now
    if db.is_remote():
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(cache, f)
            os.replace(tmp, path)
        except OSError:
            pass
    # 복사하지 않음: _latest_order 가 assign 으로 새 표를 만들어 원본을 바꾸지 않음 (메모리 절약)
    return cache["base"], cache["regions"], cache["licenses"]


def load_dataset() -> pd.DataFrame:
    """공고 + 기초금액 + 예정가격 + 개찰결과 + 낙찰자를 공고 단위로 합친 표.

    같은 프로세스에서 1분 안에 다시 부르면 메모리 값을 재사용 (원격 DB 전송량 절약)."""
    import time
    key = (db.url(), db.WRITES["n"])
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    df = _build_dataset()
    _mem.clear()
    _mem[key] = (time.time(), df)
    return df


def _build_dataset() -> pd.DataFrame:
    df, regs, lics = _raw_frames()
    if df.empty:
        return df
    df = _latest_order(df)
    # 참가가능지역·면허제한 (공고별로 여러 개 → 쉼표로 묶음)
    try:
        chk = db.query_df("SELECT bid_no, op FROM limit_checks")
    except Exception:  # noqa: BLE001
        chk = pd.DataFrame(columns=["bid_no", "op"])
    for table, col, t in (("regions", "region", regs), ("licenses", "license", lics)):
        out = "allowed_regions" if table == "regions" else "licenses"
        if len(t):
            # 정정공고로 차수가 바뀌어도 이어지도록 공고번호 기준, 자료가 있는 가장 최신 차수 사용
            t = t.assign(_no=t["bid_key"].str.rsplit("-", n=1).str[0],
                         _ord=pd.to_numeric(t["bid_key"].str.rsplit("-", n=1).str[1], errors="coerce").fillna(0))
            t = t[t["_ord"] == t.groupby("_no")["_ord"].transform("max")]
            if table == "licenses":
                grp = t[col].str.extract(r"^\[G([^\]]*)\]\s*", expand=False)
                clean = t[col].str.replace(r"^\[G[^\]]*\]\s*", "", regex=True)
                t = t.assign(**{col: clean, "_grp": grp})
                # 면허 묶음: 같은 묶음 안 면허는 모두 있어야, 다른 묶음끼리는 하나만 맞으면 참가 가능
                # (예전 자료처럼 묶음 번호가 없으면 면허마다 따로 묶음 = 하나만 맞으면 됨)
                t = t.assign(_grp=t["_grp"].fillna("x" + t[col]))
                groups = t.groupby("_no").apply(
                    lambda g: tuple(tuple(sorted(set(v))) for _, v in g.groupby("_grp")[col]), include_groups=False)
                df["license_groups"] = df["bid_no"].map(groups)
            joined = t.groupby("_no")[col].agg(lambda x: ", ".join(sorted(set(x))))
        else:
            joined = pd.Series(dtype=str)
            if table == "licenses":
                df["license_groups"] = None
        df[out] = df["bid_no"].map(joined).fillna("")
        # 그 공고 등록일의 자료를 받았거나 공고번호로 직접 확인했으면 '확인됨' (빈 값 = 제한 없음)
        days = db.done_days(table)
        checked = set(chk.loc[chk["op"] == table, "bid_no"])
        df[out + "_known"] = df["notice_dt"].astype(str).str[:10].isin(days) | (df[out] != "") | \
            df["bid_no"].isin(checked)
    # 국방(D2B) 공고: 추정가격 10억 미만은 지역제한이 원칙 → 제한 목록이 비어 있으면 '제한없음'으로 보지 않고
    # 공사 위치(지역)로 판단하게 함 (10억 이상은 전국)
    est_or_base = df["est_price"].fillna(df["base_amt_notice"] / 1.1)
    d2b_small = df["bid_key"].astype(str).str.startswith("D2B") & (df["allowed_regions"] == "") & \
        (est_or_base.isna() | (est_or_base < D2B_NATIONWIDE_FROM))
    df.loc[d2b_small, "allowed_regions_known"] = False
    df["base_amt"] = df["base_amt_open"].fillna(df["base_amt_notice"])
    # 국방 공고 낙찰하한율은 금액과 상관없이 88.745% (2026-11-25 대표 확인) → 항상 이 값으로 계산.
    # 공고 상세 값이 다르면 표시해서 사용자가 확인하게 함.
    d2b_ = df["bid_key"].astype(str).str.startswith("D2B")
    df["lower_rate_api"] = df["lower_rate"].where(d2b_)
    df["lower_rate_default"] = d2b_ & df["lower_rate"].isna()
    df["lower_rate_diff"] = d2b_ & df["lower_rate"].notna() & ((df["lower_rate"] - D2B_DEFAULT_LOWER_RATE).abs() > 1e-6)
    df.loc[d2b_, "lower_rate"] = D2B_DEFAULT_LOWER_RATE
    # 예정가격: 예비가격 상세(정확) → 없으면 낙찰률·1순위 투찰률로 역산 (금액 ÷ 예정가격 대비 비율, 소수 3자리라 오차 ±0.001%p 미만)
    df["plan_price"] = pd.to_numeric(df["plan_price"], errors="coerce").astype(float)
    src = pd.Series(np.where(df["plan_price"].notna(), "예비가격", ""), index=df.index, dtype=object)
    for amt, rate, label in (("win_amt", "win_rate", "낙찰률 역산"), ("first_amt", "first_rate", "투찰률 역산")):
        if amt in df and rate in df:
            r_ = pd.to_numeric(df[rate], errors="coerce")
            ok = df["plan_price"].isna() & pd.to_numeric(df[amt], errors="coerce").notna() & r_.between(50, 110)
            if ok.any():
                df["plan_price"] = df["plan_price"].where(
                    ~ok, (pd.to_numeric(df[amt], errors="coerce") / (r_ / 100)).round())
                src = src.where(~ok, label)
    df["plan_source"] = src
    df["a_value"] = df["a_value"].fillna(0)
    df["participants"] = df["participants_open"].where(df["participants_open"] > 0,
                                                       df["participants_final"])
    # 기관별 과거 참가업체 수: 공공 공사는 보통 수백 곳 → 늘 소수만 들어오는 기관은 협력업체 등록 등 별도 자격 필요로 봄
    # (수의계약은 원래 1~2곳이라 빼고, 일반·제한경쟁 공고만으로 판단)
    cm = df["contract_method"].fillna("")
    pp = df[(df["participants"] > 0) & cm.str.contains("일반경쟁|제한경쟁", regex=True)]
    og = pp.groupby("org")["participants"].agg(["median", "size"])
    df["org_part_median"] = df["org"].map(og["median"])
    df["org_part_n"] = df["org"].map(og["size"]).fillna(0)
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


def _latest_order(df: pd.DataFrame) -> pd.DataFrame:
    """정정공고(차수 000→001…)는 같은 공고번호의 최신 차수 하나만 남기고,
    최신 차수에 없는 정보(기초금액·개찰결과 등)는 다른 차수에서 채운다. 취소공고는 제외."""
    df = df.assign(_ord=pd.to_numeric(df["bid_ord"], errors="coerce").fillna(0))
    dup = df["bid_no"].duplicated(keep=False)
    if dup.any():
        multi = df[dup].sort_values(["bid_no", "_ord"])
        fill_cols = [c for c in multi.columns if c not in ("bid_key", "bid_no", "bid_ord", "_ord", "ntce_kind")]
        # 같은 공고번호 안에서 최신 차수 기준으로 빈 값을 이전 차수 값으로 채움
        multi[fill_cols] = multi.groupby("bid_no", sort=False)[fill_cols].ffill()
        latest = multi.groupby("bid_no", sort=False).tail(1)
        df = pd.concat([df[~dup], latest], ignore_index=True)
    kind = df["ntce_kind"].fillna("") if "ntce_kind" in df else pd.Series("", index=df.index)
    df = df[~kind.str.contains("취소", regex=False)]
    return df.drop(columns="_ord").reset_index(drop=True)


D2B_DEFAULT_LOWER_RATE = 88.745
D2B_NATIONWIDE_FROM = 1_000_000_000   # 국방 공고는 추정가격 10억 이상만 전국, 미만은 지역제한


def is_d2b(key) -> bool:
    """국방전자조달(방위사업청) 공고인지 (공고번호가 D2B 로 시작)."""
    return str(key or "").startswith("D2B")


LOWEST_NOTE = ("최저가 내역서 입찰 — 사정율 추천이 맞지 않음. 내역서·도면을 검토해 견적(원가+최소 이윤)을 "
               "산출하고, 가장 낮은 금액이 낙찰")


def lowest_price(df: pd.DataFrame) -> pd.Series:
    """최저가로 낙찰자를 정하는 공고: 계약방법에 '최저가'가 있거나, 늘 소수만 참가하는 기관(대학·민간 등 —
    울산이면 참가 가능하지만 최저가 입찰, 2026-11-24 대표 확인)의 경쟁입찰."""
    txt = df["method"].fillna("").astype(str) + " " + df["contract_method"].fillna("").astype(str) \
        if "method" in df and "contract_method" in df else pd.Series("", index=df.index)
    lp = txt.str.contains("최저가", regex=False)
    if "detail_bid" in df:
        # 내역입찰 = 내역서·도면 검토 후 견적 (적격심사 공고는 사정율 방식 그대로)
        lp |= (df["detail_bid"].fillna("") == "Y") & ~txt.str.contains("적격", regex=False)
    if "org_part_median" in df and "org_part_n" in df:
        lp |= (df["org_part_n"].fillna(0) >= 3) & (df["org_part_median"] <= SMALL_ORG_PARTICIPANTS) & \
            txt.str.contains("경쟁", regex=False)
    return lp


def region_label(r) -> str:
    """참가지역 표시: 지역제한 목록 → 지역의무공동도급이면 '현장지역+전국(○%)' → 제한없음/확인중."""
    allowed = str(r.get("allowed_regions") or "")
    if allowed:
        return allowed
    if str(r.get("joint_duty") or "") == "Y":
        site = str(r.get("region") or "").split(" ")[0] or "현장지역"
        rate = r.get("joint_rate")
        rt = f" {float(rate):.0f}%" if rate is not None and pd.notna(rate) else ""
        return f"{site}+전국 (지역의무공동도급{rt})"
    return "제한없음" if r.get("allowed_regions_known") else "확인중"


def reg_deadline(df: pd.DataFrame) -> pd.Series:
    """입찰참가등록 마감 — 국방 공고만 의미 있음 (나라장터는 공고별 참가등록이 사실상 없음)."""
    if "qual_dt" not in df:
        return pd.Series(pd.NaT, index=df.index)
    q = pd.to_datetime(df["qual_dt"], errors="coerce", format="mixed")
    return q.where(df["bid_key"].astype(str).str.startswith("D2B"))


def eligible(df: pd.DataFrame, company_region: str | None = None,
             company_licenses: list[str] | None = None) -> pd.Series:
    """우리 회사가 참가 가능한 공고인지.

    - 지역: 참가가능지역이 없으면(전국) 가능, 있으면 우리 지역이 포함돼야 가능.
      아직 지역 정보를 못 받은 공고는 공사지역·기관명에 우리 지역이 있으면 가능으로 봄.
    - 면허: 면허제한이 확인되고 값이 있으면 우리 면허가 포함돼야 가능.
    - 수의시담(지정 업체와 협상)은 제외.
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
        simple = df["licenses"].str.contains("|".join(map(re.escape, lic)), regex=True)
        if "license_groups" in df:
            grp = df["license_groups"]
            has_grp = grp.map(lambda x: isinstance(x, tuple) and len(x) > 0)
            parts = partner_keywords()
            by_grp = pd.Series([(license_ok(x, lic) or license_partner_ok(x, lic, parts)) if h else True
                                for x, h in zip(grp, has_grp)], index=df.index)
            ok &= ~has | (has_grp & by_grp) | (~has_grp & simple)
        else:
            ok &= ~has | simple
    if "method" in df:
        # 수의시담: 발주기관이 정한 업체와 협상하는 수의계약 → 우리가 자유롭게 참가할 수 없음
        ok &= ~df["method"].fillna("").str.contains("수의시담", regex=False)
    ok &= ~limit_reasons(df).astype(bool)
    return ok


def _lic_core(x: str) -> str:
    """'전기공사업/0037' → '전기공사업' (코드·공백 제거해서 비교)."""
    return re.sub(r"/\d+$", "", x.strip()).replace(" ", "")


def license_ok(groups, company_licenses: list[str]) -> bool:
    """면허제한 묶음 판단: 어떤 묶음 하나의 면허를 '모두' 가지고 있으면 참가 가능.

    한 항목에 허용업종 목록('[...]')이 붙어 있으면 그중 하나만 있어도 그 항목은 충족."""
    if not groups:
        return True
    mine = [_lic_core(x) for x in company_licenses if x]
    if not mine:
        return True

    def entry_ok(e: str) -> bool:
        alts = [e.split(" [")[0]] + re.findall(r"[^\[\],]+", e[len(e.split(" [")[0]):])
        for alt in alts:
            core = _lic_core(alt)
            if core and any(m in core or core in m for m in mine):
                return True
        return False
    return any(all(entry_ok(e) for e in g) for g in groups)


def partner_keywords() -> list[str]:
    """공동도급 협정으로 채울 수 있는 면허 (설정, 기본: 소방 — 2026-11-24 대표: 전기/소방 공고는 협정 가능)."""
    v = db.get_kv("partner_licenses", None)
    return [x.strip() for x in (v if v is not None else "소방").split(",") if x.strip()]


def license_partner_ok(groups, company_licenses: list[str], partners: list[str]) -> bool:
    """우리 면허 + 협정 업체 면허(예: 소방)로 어떤 묶음을 채울 수 있는지. 우리 면허가 묶음에 하나는 있어야 함."""
    if not groups or not partners:
        return False
    mine = [_lic_core(x) for x in company_licenses if x]

    def ours(e: str) -> bool:
        return license_ok(((e,),), company_licenses)

    def partner(e: str) -> bool:
        return any(p in e for p in partners)
    return bool(mine) and any(any(ours(e) for e in g) and all(ours(e) or partner(e) for e in g) for g in groups)


def needs_partner(df: pd.DataFrame, company_licenses: list[str] | None = None) -> pd.Series:
    """우리 면허만으로는 안 되고 협정(공동도급)이 있어야 참가 가능한 공고."""
    lic = company_licenses if company_licenses is not None else db.company()["licenses"]
    if "license_groups" not in df or not lic:
        return pd.Series(False, index=df.index)
    parts = partner_keywords()
    return pd.Series([isinstance(g, tuple) and len(g) > 0 and not license_ok(g, lic) and license_partner_ok(g, lic, parts)
                      for g in df["license_groups"]], index=df.index)


# 공고명에 실적·기술자 보유 조건이 적힌 경우 (예: ★기술자 및 실적보유★, ★전차선실적및기술자보유★, [실적제한])
PERF_TITLE = r"실적\s*(?:및|보유|제한|있는|소지)|기술자\s*(?:및|보유)|실적[^\s★\]]{0,8}보유"


def _ability_needed(x) -> float | None:
    """시공능력평가금액 제한 목록에서 요구 금액(원) — 가장 큰 숫자."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]{6,}", str(x or ""))]
    return max(nums) if nums else None


SMALL_ORG_PARTICIPANTS = 30   # 기관 과거 참가업체 중앙값이 이 이하이면 별도 자격 기관으로 봄


def limit_reasons(df: pd.DataFrame) -> pd.Series:
    """우리가 못 들어가는 이유(실적제한·PQ·지명경쟁·시공능력 부족). 없으면 ''. 회사 설정으로 일부 허용 가능."""
    out = pd.Series("", index=df.index, dtype=object)
    if df.empty:
        return out
    allow_perf = db.get_kv("allow_perf_limit", "0") == "1"
    try:
        ability = float(db.get_kv("company_ability", "") or 0)
    except ValueError:
        ability = 0.0
    name = df["name"].fillna("") if "name" in df else pd.Series("", index=df.index)

    def add(mask, text):
        nonlocal out
        out = out.where(~mask, out + np.where(out == "", "", " · ") + text)
    if not allow_perf:
        perf = (df["perf_limit"].fillna("") == "Y") if "perf_limit" in df else pd.Series(False, index=df.index)
        perf |= name.str.contains(PERF_TITLE, regex=True)
        add(perf, "실적제한")
    if "pq" in df:
        add(df["pq"].fillna("") == "Y", "PQ심사")
    if "designated" in df or "contract_method" in df:
        des = (df["designated"].fillna("") == "Y") if "designated" in df else pd.Series(False, index=df.index)
        if "contract_method" in df:
            des |= df["contract_method"].fillna("").str.contains("지명", regex=False)
        add(des, "지명경쟁")
    if "ability_list" in df and ability > 0:
        need = df["ability_list"].map(_ability_needed)
        add(need.notna() & (need > ability), "시공능력 부족")
    # 소수 참가 기관: 과거 공고(3건 이상)에 늘 30곳 이하만 참가 → 협력업체 등록 등 별도 자격이 있는 기관으로 추정
    # (2026-11-24 대표 확인: 이런 공고는 못 넣음). 설정의 '참가 가능 기관'에 넣으면 제외하지 않음.
    if "org_part_median" in df:
        allow = [x.strip() for x in (db.get_kv("allow_orgs", "") or "").split(",") if x.strip()]
        few = (df["org_part_n"].fillna(0) >= 3) & (df["org_part_median"] <= SMALL_ORG_PARTICIPANTS)
        if "contract_method" in df:   # 수의계약 공고 자체는 이 기준으로 판단하지 않음
            few &= df["contract_method"].fillna("").str.contains("경쟁", regex=False) | (df["contract_method"].fillna("") == "")
        if allow:
            few &= ~df["org"].fillna("").map(lambda o: any(x in o for x in allow))
        # 우리 지역(울산) 현장·기관이면 참가 가능 (2026-11-24 대표 확인: 울산대·울산과학대 등 울산이면 들어갈 수 있음)
        reg = db.company()["region"] or ""
        if reg:
            place = (df["region"].fillna("") if "region" in df else "") + " " + df["org"].fillna("") + " " + \
                (df["demand_org"].fillna("") if "demand_org" in df else "")
            few &= ~place.str.contains(reg, regex=False)
        add(few, "소수 참가 기관(협력업체 등록 등 별도 자격 추정)")
    block = [x.strip() for x in (db.get_kv("block_orgs", "") or "").split(",") if x.strip()]
    if block and "org" in df:
        add(df["org"].fillna("").map(lambda o: any(x in o for x in block)), "제외 기관(설정)")
    # 내역입찰(내역서 제출)인데 현장이 다른 지역 → 민간·대학 등 견적형 공고가 대부분이라 제외 (2026-11-25 대표 확인:
    # 서울 등우빌딩 기숙사·총신대 기숙사 "안 됨"). 울산 현장이면 최저가 내역서 입찰(💲)로 참가 가능.
    if "detail_bid" in df and db.get_kv("allow_detail_bid_elsewhere", "0") != "1":
        reg_ = db.company()["region"] or ""
        site_ = df["region"].fillna("") if "region" in df else pd.Series("", index=df.index)
        allowed_ = df["allowed_regions"].fillna("") if "allowed_regions" in df else pd.Series("", index=df.index)
        local_ = site_.str.contains(reg_, regex=False) | allowed_.str.contains(reg_, regex=False) if reg_ else False
        add((df["detail_bid"].fillna("") == "Y") & ~local_, "내역입찰(다른 지역 — 견적형)")
    # 지역의무공동도급: 공사 현장 지역 업체와 반드시 공동도급 (예: 부산 공사 → 부산 업체 49% 이상 + 전국 업체)
    # 우리 지역 공사면 우리가 지역 업체라 문제없음. 다른 지역이면 그 지역 업체와 손잡아야 해서 기본은 제외.
    if "joint_duty" in df and db.get_kv("allow_joint_duty", "0") != "1":
        reg = db.company()["region"] or ""
        site = df["region"].fillna("") if "region" in df else pd.Series("", index=df.index)
        jd = (df["joint_duty"].fillna("") == "Y") & ~(site.str.contains(reg, regex=False) if reg else False)
        add(jd, "지역의무공동도급(현장 지역 업체와 공동도급 필요)")
    return out


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


def n_windows(hist: pd.DataFrame) -> int:
    """낙찰 구간이 있는 공고 수 (빠른 계산)."""
    if hist is None or hist.empty:
        return 0
    a = hist["sajung"].to_numpy(dtype=float, na_value=np.nan)
    b = hist["winner_sajung"].to_numpy(dtype=float, na_value=np.nan)
    return int(np.count_nonzero(b >= a))  # NaN 비교는 False


def win_windows(hist: pd.DataFrame) -> pd.DataFrame:
    """과거 공고별 '낙찰 구간' [실제 사정율, 1순위 사정율).

    내 투찰 사정율 s 가 이 구간 안에 있었다면:
      - s ≥ 실제 사정율 → 투찰금액이 하한 이상(유효)
      - s < 1순위 사정율 → 실제 1순위보다 낮은 금액 → 내가 1순위
    """
    h = hist.dropna(subset=["sajung", "winner_sajung"])
    h = h[h["winner_sajung"] >= h["sajung"]]  # 데이터 오류 제거
    return h[["bid_key", "sajung", "winner_sajung"]]


def win_curve(hist: pd.DataFrame, lo: float = 98.0, hi: float = 102.0, step: float = 0.01,
              width: float | None = None) -> pd.DataFrame:
    """사정율별 과거 낙찰확률·하한통과확률."""
    grid = np.round(np.arange(lo, hi + 1e-9, step), 4)
    av = hist["sajung"].to_numpy(dtype=float, na_value=np.nan) if len(hist) else np.array([])
    bv = hist["winner_sajung"].to_numpy(dtype=float, na_value=np.nan) if len(hist) else np.array([])
    ok = bv >= av
    w = pd.DataFrame({"sajung": av[ok], "winner_sajung": bv[ok]})
    if w.empty:
        return pd.DataFrame({"사정율": grid, "낙찰확률": np.nan, "낙찰확률(원값)": np.nan,
                             "하한통과확률": np.nan, "표본": 0})
    # 정렬 + 이진탐색으로 계산 (공고 수 × 격자 비교보다 훨씬 빠름)
    #   낙찰 = 실제 사정율 ≤ g < 낙찰자 사정율 인 공고 수 = #(a ≤ g) − #(b ≤ g)   (b ≥ a)
    a = np.sort(w["sajung"].values)
    b = np.sort(w["winner_sajung"].values)
    n_a = np.searchsorted(a, grid, side="right")
    n_b = np.searchsorted(b, grid, side="right")
    win = (n_a - n_b) / len(w) * 100
    valid = n_a / len(w) * 100
    # 한 공고의 낙찰 구간은 매우 좁아서 그래프가 들쭉날쭉함 → 범위 평균으로 평활화
    # 표본이 적을수록 더 넓게 평균 (우연히 한 번 맞은 지점에 끌려가지 않도록)
    n = len(w)
    if width is None:
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


def likely_competitors(ranks: pd.DataFrame, row, df: pd.DataFrame | None = None, top: int = 5) -> tuple[str, pd.DataFrame]:
    """이 공고에 들어올 가능성이 높은 경쟁사: 같은 기관 → 우리 지역 지역제한 공고 → 전체 순으로
    과거 개찰순위(상위권)에 자주 나온 업체와 그 업체가 보통 노리는 사정율."""
    if ranks is None or ranks.empty:
        return "", pd.DataFrame()
    pools = []
    region = db.company()["region"]
    idx = _ranks_index(ranks, df, region)
    org_r = idx["by_org"].get(row["org"])
    if org_r is not None and org_r["bid_key"].nunique() >= 3:
        pools.append((f"같은 기관({row['org']}) 공고 {org_r['bid_key'].nunique()}건", org_r))
    if df is not None and region and is_local_bid(row, region):
        loc_r = idx["loc"]
        if loc_r is not None and loc_r["bid_key"].nunique() >= 3:
            pools.append((f"{region} 지역제한 공고 {loc_r['bid_key'].nunique()}건", loc_r))
    pools.append((f"전체 공고 {ranks['bid_key'].nunique()}건", ranks))
    label, pool = pools[0]
    ours = db.company()["bizno"]
    # 지역·전체 풀은 공고마다 같으므로 요약을 한 번만 계산해 재사용 (기관 풀은 작아서 매번 계산)
    ck = (id(ranks), len(ranks), label.split(" 공고")[0] if not label.startswith("같은 기관") else None)
    if ck[2] is not None and ck in _comp_cache:
        g = _comp_cache[ck]
    else:
        g = _summarise_competitors(pool)
        if ck[2] is not None:
            if len(_comp_cache) > 16:
                _comp_cache.clear()
            _comp_cache[ck] = g
    if ours:
        g = g[g["_bizno"].fillna("").str.replace("-", "") != ours]
    g = g.sort_values(["등장", "10위 이내"], ascending=False).head(top).drop(columns="_bizno")
    return label, g.round(3)


_comp_cache: dict = {}
_ranks_idx_cache: dict = {}


def _ranks_index(ranks: pd.DataFrame, df: pd.DataFrame | None, region: str) -> dict:
    """개찰순위를 기관별로 나누고 지역제한 공고 부분을 한 번만 계산."""
    key = (id(ranks), len(ranks), id(df), region)
    hit = _ranks_idx_cache.get(key)
    if hit is not None and hit["ranks"] is ranks:
        return hit
    loc = None
    if df is not None and region:
        loc_keys = set(_context(df, company_keywords(), region)["loc"]["bid_key"])
        loc = ranks[ranks["bid_key"].isin(loc_keys)]
    out = {"ranks": ranks, "by_org": dict(tuple(ranks.groupby("org"))), "loc": loc}
    if len(_ranks_idx_cache) > 4:
        _ranks_idx_cache.clear()
    _ranks_idx_cache[key] = out
    return out


def _summarise_competitors(pool: pd.DataFrame) -> pd.DataFrame:
    key = pool["bizno"].where(pool["bizno"].fillna("") != "", pool["company"])
    return pool.assign(_k=key).groupby("_k").agg(**{
        "업체명": ("company", "last"),
        "_bizno": ("bizno", "last"),
        "등장": ("bid_key", "nunique"),
        "10위 이내": ("rank", lambda x: int((x <= 10).sum())),
        "평균 사정율": ("투찰사정율", "mean"),
        "편차": ("투찰사정율", "std"),
    }).reset_index(drop=True)


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
    return same if n_windows(same) >= 15 else h


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
        if n_windows(b) >= min_n:
            return b
    return h


def _rng(x) -> pd.Series | float:
    """예가 범위 폭(%p): ±2% → 4.0, ±3% → 6.0 (실제 사정율 퍼짐이 달라서 같은 범위끼리 비교)."""
    if isinstance(x, pd.DataFrame):
        if "range_begin" not in x or "range_end" not in x:
            return pd.Series(np.nan, index=x.index)
        return (x["range_begin"].abs() + x["range_end"].abs()).round(1)
    try:
        rb, re_ = x.get("range_begin"), x.get("range_end")
        return round(abs(float(rb)) + abs(float(re_)), 1) if pd.notna(rb) and pd.notna(re_) else np.nan
    except (TypeError, ValueError):
        return np.nan


def same_range(h: pd.DataFrame, row, min_n: int = 20) -> pd.DataFrame:
    """예가 범위가 같은 공고만 (충분할 때)."""
    r = _rng(row)
    if pd.isna(r) or h.empty:
        return h
    c = h[_rng(h) == r]
    return c if n_windows(c) >= min_n else h


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


def local_mask(df: pd.DataFrame, region: str | None = None) -> pd.Series:
    """우리 지역 공고: 참가지역에 우리 지역이 들어 있거나, 공사지역·발주기관이 우리 지역."""
    reg = region if region is not None else db.company()["region"]
    if not reg or df.empty:
        return pd.Series(True, index=df.index)
    place = df["region"].fillna("") + " " + df["org"].fillna("") + " " + df["demand_org"].fillna("")
    m = place.str.contains(reg, regex=False)
    if "allowed_regions" in df:
        m |= df["allowed_regions"].fillna("").str.contains(reg, regex=False)
    return m


def local_region_history(done: pd.DataFrame, region: str) -> pd.DataFrame:
    """우리 지역 지역제한 공고 (지역 정보를 아직 못 받은 공고는 공사지역·기관명으로 판단)."""
    allowed = done["allowed_regions"] if "allowed_regions" in done else pd.Series("", index=done.index)
    known = done["allowed_regions_known"] if "allowed_regions_known" in done else pd.Series(False, index=done.index)
    place = done["region"].fillna("") + " " + done["org"].fillna("") + " " + done["demand_org"].fillna("")
    m = allowed.str.contains(region, regex=False) | (~known & (allowed == "") & place.str.contains(region, regex=False))
    return done[m]


_ctx_cache: dict = {}


def _context(df: pd.DataFrame, keywords, region: str):
    """같은 df·업종·지역에 대한 공통 부분집합을 한 번만 계산해 재사용 (공고마다 15만 건을 다시 거르지 않도록)."""
    key = (id(df), len(df), tuple(keywords or ()), region)
    hit = _ctx_cache.get(key)
    if hit is not None and hit["df"] is df:
        return hit
    done = df[df["sajung"].notna()]
    if keywords and df.attrs.get("_ind_key") == tuple(keywords) and "_ind" in df:
        ind = done[done["_ind"]]          # 백테스트: 미리 계산해 둔 업종 표시 사용
    else:
        ind = apply_filters(done, keywords=keywords) if keywords else done
    if n_windows(ind) < 30:
        ind = done
    if region and df.attrs.get("_loc_key") == region and "_loc" in df:
        loc = ind[ind["_loc"]]
    else:
        loc = local_region_history(ind, region) if region else ind.iloc[0:0]
    # 계산에 필요한 열만 남겨 가볍게 (넓은 표를 반복 복사하는 비용 절감)
    cols = [c for c in ("bid_key", "name", "org", "open_date", "sajung", "winner_sajung",
                        "participants", "base_amt", "first_won", "range_begin", "range_end") if c in df]
    done, ind, loc = done[cols], ind[cols], loc[cols]
    ctx = {"df": df, "done": done, "ind": ind, "loc": loc, "by_org": dict(tuple(ind.groupby("org")))}
    if len(_ctx_cache) > 8:
        _ctx_cache.clear()
    _ctx_cache[key] = ctx
    return ctx


COMPARABLE_RATIO = 0.3   # 예상 참가업체 수의 30% 이상이 들어온 공고만 비교 (경쟁 강도가 비슷한 공고끼리)


def expected_participants(tier_list) -> float | None:
    """가장 가까운 단계(같은 기관 → 지역 → 전국)에서 참가업체 수 중앙값."""
    for _, h in reversed(tier_list):
        p = h["participants"].dropna() if "participants" in h else pd.Series(dtype=float)
        p = p[p > 0]
        if len(p) >= 5:
            return float(p.median())
    return None


FOCUS_MAX_PARTICIPANTS = 150   # 같은 유형 참가 중앙값이 이 이하면 '★ 집중' (2026-11-25 공정 시험: ★ 공고 1순위율 4.6% vs 나머지 2.1%)


def _comp_group(d) -> pd.Series | str:
    """참가업체 수를 좌우하는 공고 유형: 지역범위 × 계약방식 × 금액대."""
    region = db.company()["region"] or "울산"

    def one(allowed, cm, amt):
        allowed, cm = str(allowed or ""), str(cm or "")
        scope = f"{region}제한" if region in allowed else ("타지역제한" if allowed else "전국")
        meth = "수의" if "수의" in cm else ("최저가" if "최저가" in cm else "경쟁")
        try:
            a_ = float(amt)
        except (TypeError, ValueError):
            a_ = float("nan")
        band = ("?" if a_ != a_ else "~5천" if a_ < 5e7 else "~1억" if a_ < 1e8 else "~3억" if a_ < 3e8
                else "~10억" if a_ < 1e9 else "10억~")
        return f"{scope}|{meth}|{band}"
    if isinstance(d, pd.DataFrame):
        amt = d["base_amt"].fillna(d["est_price"] * 1.1) if "est_price" in d else d["base_amt"]
        cm = d["contract_method"].fillna("").astype(str) + " " + d["method"].fillna("").astype(str)
        return pd.Series([one(x, y, z) for x, y, z in zip(d["allowed_regions"].fillna(""), cm, amt)], index=d.index)
    amt = d.get("base_amt")
    if (amt is None or pd.isna(amt)) and pd.notna(d.get("est_price")):
        amt = d["est_price"] * 1.1
    return one(d.get("allowed_regions"), f"{d.get('contract_method') or ''} {d.get('method') or ''}", amt)


_COMP_CACHE: dict = {}


def expected_competition(df: pd.DataFrame, row, keywords=None) -> float | None:
    """과거 같은 유형(지역범위·계약방식·금액대) 공고의 참가업체 수 중앙값 (2026-11-25: 기관·업종 기준 예측은
    공정 시험에서 거의 안 맞아서(상관 0.24) 교체)."""
    try:
        key = _comp_group(row)
        ck = (id(df), len(df))
        med = _COMP_CACHE.get(ck)
        if med is None:
            h = df[(df["participants"] > 0) & df["open_date"].notna()]
            med = h.groupby(_comp_group(h))["participants"].agg(["median", "size"])
            if len(_COMP_CACHE) > 4:
                _COMP_CACHE.clear()
            _COMP_CACHE[ck] = med
        if key in med.index and med.at[key, "size"] >= 5:
            return float(med.at[key, "median"])
        return None
    except Exception:  # noqa: BLE001
        return None


def expected_competition_old(df: pd.DataFrame, row, keywords=None) -> float | None:
    """기초금액 공개 전에도: 과거 비슷한 공고(같은 기관 → 울산 지역제한 → 전국 같은 업종·금액대)의 참가업체 수 중앙값."""
    try:
        if pd.isna(row.get("base_amt")) and pd.notna(row.get("est_price")):
            row = {**dict(row), "base_amt": row["est_price"] * 1.1}
        return expected_participants(tiers(df, row, keywords))
    except Exception:  # noqa: BLE001
        return None


def comparable(h: pd.DataFrame, expected: float | None, min_n: int = 20) -> pd.DataFrame:
    """경쟁 강도가 비슷한 공고만. 참가업체가 적은 공고는 '이기는 구간'이 넓어서 섞으면 추천이 높아짐
    (2026-11-23 첫 실전: 350곳 공고에 소액·소수참가 공고가 섞여 실제보다 0.3~0.7%p 높게 추천된 원인)."""
    if expected is None or expected < 30 or "participants" not in h:
        return h
    c = h[h["participants"] >= expected * COMPARABLE_RATIO]
    return c if n_windows(c) >= min(min_n, max(n_windows(h), 1)) else h


def tiers(df: pd.DataFrame, row, keywords=None) -> list[tuple[str, pd.DataFrame]]:
    """비교 단계: [전국, (우리 지역), (같은 기관)] — 넓은 것부터. 모두 참가업체 수가 비슷한 공고만."""
    region = db.company()["region"]
    ctx = _context(df, keywords, region)
    ind = ctx["ind"]
    out = [("전국 같은 업종", _band(ind, row["base_amt"], 200))]
    if region and is_local_bid(row, region):
        loc = ctx["loc"]
        if n_windows(loc):
            out.append((f"{region} 지역제한 공고", _band(loc, row["base_amt"], 100)))
    org = ctx["by_org"].get(row["org"], ind.iloc[0:0])
    if n_windows(org) >= 5:
        out.append((f"같은 기관({row['org']})", org))
    exp = expected_participants(out)
    out = [(name, comparable(h, exp, 5 if name.startswith("같은 기관") else 20)) for name, h in out]
    return [t for i, t in enumerate(out) if i == 0 or n_windows(t[1]) >= 5]


def blend(tier_list) -> tuple[pd.DataFrame, list[float]]:
    """넓은 단계부터 차례로 섞기: p = (n·p_하위 + 100·p_상위) / (n + 100). 각 단계 반영 비중도 반환."""
    curve = win_curve(tier_list[0][1])
    p = curve["낙찰확률"].fillna(0)
    weights = [1.0]
    for _, h in tier_list[1:]:
        n = n_windows(h)
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


# 추천 방식 (자동 검증 결과에 따라 바뀜, 기본 = 단계별 혼합)
STRATEGIES = {
    "blend": "① 현재 방식 (기관+울산+전국)",
    "blend_noregion": "② 울산 단계 없이 (기관+전국)",
    "national": "③ 전국 같은 업종만",
    "region_only": "④ 울산 지역제한 공고만",
    "mean_sajung": "⑤ 단순 평균 (사정율 평균)",
    "mean_winner": "⑥ 과거 1순위 사정율 평균",
    "blend_range": "⑦ 현재 방식 + 예가범위 맞춤",
    "smooth15": "⑧ 넓게 평활 0.15%p",
    "smooth30": "⑨ 넓게 평활 0.30%p",
}
LABEL_TO_MODE = {v: k for k, v in STRATEGIES.items()}


def current_strategy() -> str:
    m = db.get_kv("strategy", "blend") or "blend"
    return m if m in STRATEGIES else "blend"


STICKY_RATIO = 0.9  # 이전 추천의 확률이 최고의 90% 이상이면 이전 추천 유지 (사소한 데이터 변화에 흔들리지 않게)


def pick_bid(df: pd.DataFrame, row, keywords=None, use_region: bool = True,
             mode: str | None = None, prev: float | None = None) -> dict | None:
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
    done = _context(df, keywords, db.company()["region"])["done"]
    tl = tiers(df, row, keywords)
    if not use_region:
        tl = [t for t in tl if "지역제한" not in t[0]]
    if n_windows(tl[0][1]) < 5:
        s_ = float(done["sajung"].median()) if len(done) else 100.0
        return {"사정율": round(s_, 2), "투찰금액": bid_amount(base, s_, lr, a_val), "신뢰도": "낮음",
                "근거": "과거 사정율 중앙값(데이터 부족)", "낙찰확률": None, "무작위 대비": None, "후보": [],
                "단계": [], "근거상세": evidence(done, round(s_, 2))}
    mode = mode or current_strategy()
    reg_tiers = [t for t in tl if "지역제한" in t[0]]
    if mode == "blend_noregion":
        tl = [t for t in tl if "지역제한" not in t[0]]
    elif mode == "national":
        tl = tl[:1]
    elif mode == "region_only" and reg_tiers and n_windows(reg_tiers[0][1]) >= 5:
        tl = reg_tiers
    elif mode == "blend_range":
        tl = [(n, same_range(h, row, 5 if n.startswith("같은 기관") else 20)) for n, h in tl]
    curve, weights = blend(tl)
    if mode in ("smooth15", "smooth30"):
        curve = win_curve(tl[0][1], width=0.15 if mode == "smooth15" else 0.3)
    fixed_s = None
    if mode == "mean_sajung":
        fixed_s = round(float(tl[0][1]["sajung"].mean()), 2)
    elif mode == "mean_winner" and tl[0][1]["winner_sajung"].notna().any():
        fixed_s = round(float(tl[0][1]["winner_sajung"].mean()), 2)
    floor_amt = pure_cost_min(row)
    floor_s = min_sajung_for_amount(base, lr, a_val, floor_amt) if floor_amt else None
    if floor_s is not None:
        # 순공사원가 98% 미만 사정율은 후보에서 제외
        curve = curve.assign(낙찰확률=curve["낙찰확률"].where(curve["사정율"] >= floor_s - 1e-9, 0))
    picks = best_points(curve, top=3)
    if not picks:
        picks = [{"사정율": floor_s or 100.0, "낙찰확률": 0.0, "하한통과확률": 0.0}]
    s_ = round(picks[0]["사정율"], 2)
    kept = False
    if prev is not None and pd.notna(prev) and fixed_s is None and abs(prev - s_) >= 0.005:
        hit = curve.iloc[(curve["사정율"] - prev).abs().argsort()[:1]]
        p_prev = float(hit["낙찰확률"].iloc[0])
        if picks[0]["낙찰확률"] > 0 and p_prev >= picks[0]["낙찰확률"] * STICKY_RATIO:
            s_, kept = round(float(prev), 2), True
            picks = [{"사정율": s_, "낙찰확률": p_prev, "하한통과확률": float(hit["하한통과확률"].iloc[0])}] + \
                [x for x in picks if abs(x["사정율"] - s_) >= 0.15][:2]
    if fixed_s is not None:
        s_ = fixed_s
        hit = curve.iloc[(curve["사정율"] - s_).abs().argsort()[:1]]
        picks = [{"사정율": s_, "낙찰확률": float(hit["낙찰확률"].iloc[0]), "하한통과확률": 0.0}] + picks
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
        "방식": STRATEGIES[mode],
        "이전 추천 유지": kept,
    }


def pick_history() -> dict:
    """공고별 추천 이력 {bid_key: [(day, 사정율, 금액, 확률), ...]} (오래된 것부터)."""
    h = db.query_df("SELECT bid_key, day, sajung, amount, prob FROM pick_log ORDER BY bid_key, day")
    return {k: list(g[["day", "sajung", "amount", "prob"]].itertuples(index=False, name=None))
            for k, g in h.groupby("bid_key")} if len(h) else {}


def prev_pick(hist: dict, bid_key: str):
    """가장 최근 추천 사정율 (없으면 None)."""
    h = hist.get(bid_key)
    return h[-1][1] if h else None


def log_picks(df: pd.DataFrame, hist: dict | None = None) -> int:
    """개찰 전·기초금액 공개 공고의 오늘 추천을 기록 (알림·화면이 같은 금액을 보도록)."""
    now = pd.Timestamp.now()
    t = apply_filters(df, keywords=company_keywords(), eligible_only=True)
    t = t[(t["open_date"] >= now) & t["base_amt"].notna() & t["lower_rate"].notna()]
    hist = pick_history() if hist is None else hist
    rows, day, at = [], now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
    for _, r in t.iterrows():
        pk = pick_bid(df, r, bid_keywords(r), prev=prev_pick(hist, r["bid_key"]))
        if pk:
            rows.append({"bid_key": r["bid_key"], "day": day, "sajung": pk["사정율"],
                         "amount": float(pk["투찰금액"]), "prob": pk["낙찰확률"], "at": at})
    db.upsert_many("pick_log", rows)
    return len(rows)


def history_line(hist: dict, bid_key: str) -> str | None:
    """추천이 바뀐 적 있으면 '11/21 100.54% → 11/22 100.27%' 형태로."""
    h = hist.get(bid_key) or []
    out, last = [], None
    for day, s_, _, _ in h:
        if last is None or abs(s_ - last) >= 0.005:
            out.append(f"{day[5:].replace('-', '/')} {s_:.2f}%")
            last = s_
    return " → ".join(out) if len(out) > 1 else None


def pick_scorecard(df: pd.DataFrame) -> pd.DataFrame:
    """개찰 전에 기록해 둔 추천(pick_log)대로 넣었다면 결과가 어땠는지 (미래 정보 없이 공정한 성적)."""
    pl = db.query_df("SELECT bid_key, day, sajung, amount FROM pick_log")
    if pl.empty or df.empty:
        return pd.DataFrame()
    cols = ["bid_key", "bid_no", "name", "org", "open_date", "sajung", "winner_sajung", "participants",
            "base_amt", "lower_rate", "a_value", "allowed_regions", "region", "demand_org"]
    d = df[[c for c in cols if c in df]]
    v = pl.merge(d, on="bid_key", how="inner", suffixes=("_pick", ""))
    v = v[v["open_date"].notna() & (v["day"] <= v["open_date"].dt.strftime("%Y-%m-%d"))]
    v = v.sort_values("day").groupby("bid_key").tail(1)          # 개찰 전 마지막 추천
    v = v[v["sajung"].notna() & (v["open_date"] < pd.Timestamp.now())]
    if v.empty:
        return pd.DataFrame()
    s_, a, b = v["sajung_pick"], v["sajung"], v["winner_sajung"]
    v["결과"] = np.where(s_ < a, "하한 미달",
                       np.where(b.notna() & (s_ < b), "🏆 낙찰", np.where(b.notna(), "낙찰자보다 높음", "결과 대기")))
    rk = db.query_df("SELECT bid_key, amount FROM ranks WHERE rank > 0 AND amount IS NOT NULL")
    amounts = {k: np.sort(g["amount"].values) for k, g in rk.groupby("bid_key")} if len(rk) else {}

    def vrank(r):
        comp = amounts.get(r["bid_key"])
        if comp is None or not len(comp) or r["결과"] == "하한 미달":
            return np.nan
        n = 1 + int((comp < r["amount"]).sum())
        return n if n <= len(comp) else np.inf
    v["가상 순위"] = v.apply(vrank, axis=1)
    return v.sort_values("open_date", ascending=False).reset_index(drop=True)


def scorecard_summary(sc: pd.DataFrame) -> str:
    """'추천대로 넣었다면 N건 중 🏆 a · 하한 미달 b · 높음 c · 10위 이내 d' 한 줄."""
    if sc is None or sc.empty:
        return ""
    r = sc["결과"]
    vr = sc["가상 순위"]
    top10 = int(((vr > 1) & (vr <= 10)).sum())
    txt = (f"{len(sc)}건 중 🏆 낙찰 {int((r == '🏆 낙찰').sum())} · 2~10위 {top10} · "
           f"하한 미달 {int((r == '하한 미달').sum())} · 낙찰자보다 높음 {int((r == '낙찰자보다 높음').sum())}")
    med = vr[np.isfinite(vr)].median() if vr.notna().any() else np.nan
    if pd.notna(med):
        txt += f" (가상 순위 중앙 {med:.0f}위)"
    return txt


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
    if pk.get("이전 추천 유지"):
        out.append("이전 추천과 확률 차이가 10% 이내라 이전 추천을 그대로 유지 (사소한 데이터 변화에 흔들리지 않게)")
    if pk.get("방식") and pk["방식"] != STRATEGIES["blend"]:
        out.append(f"추천 방식: {pk['방식']} (자동 검증에서 성적이 가장 좋았던 방식)")
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
    # 정정공고로 차수가 바뀌어도 이어지도록 공고번호로 연결
    mb["bid_no"] = mb["bid_key"].str.rsplit("-", n=1).str[0]
    v = mb.merge(df.drop(columns=["bid_key"]), on="bid_no", how="left", suffixes=("", "_bid"))
    # 값이 하나도 없는 칸은 글자형(object)으로 읽혀 계산이 깨지므로 숫자로 맞춘다
    for c in ["amount", "sajung", "my_rank", "rec_amount", "a_value", "lower_rate", "base_amt",
              "plan_price", "first_amt"]:
        if c in v.columns:
            v[c] = pd.to_numeric(v[c], errors="coerce")
    # 분석(추천) 금액: 투찰 기록 때 저장한 추천 금액, 없으면 개찰 전에 기록된 마지막 추천(추천 성적표)
    try:
        pl = db.query_df("SELECT bid_key, day, amount AS pl_amount FROM pick_log ORDER BY bid_key, day")
        if len(pl):
            last = pl.groupby("bid_key").tail(1)
            last = last.assign(bid_no=last["bid_key"].str.rsplit("-", n=1).str[0]).drop_duplicates("bid_no", keep="last")
            v = v.merge(last[["bid_no", "pl_amount"]], on="bid_no", how="left")
            v["rec_amount"] = v["rec_amount"].fillna(pd.to_numeric(v["pl_amount"], errors="coerce"))
    except Exception:  # noqa: BLE001
        pass
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
    if n_windows(tl[0][1]) < 5:
        return out
    out["① 현재 방식 (기관+울산+전국)"] = _pick_from_curve(blend(tl)[0])
    no_reg = [t for t in tl if "지역제한" not in t[0]]
    if len(no_reg) != len(tl):
        out["② 울산 단계 없이 (기관+전국)"] = _pick_from_curve(blend(no_reg)[0])
    out["③ 전국 같은 업종만"] = _pick_from_curve(win_curve(tl[0][1]))
    reg = [t for t in tl if "지역제한" in t[0]]
    if reg and n_windows(reg[0][1]) >= 5:
        out["④ 울산 지역제한 공고만"] = _pick_from_curve(win_curve(reg[0][1]))
    # 경쟁 공고(수백 곳)는 낙찰 구간이 0.005%p 정도로 좁아 '과거에 몇 번 맞은 점'을 고르면 우연에 끌려감 →
    # 넓게 평활해서 '실제 사정율이 자주 나오고 경쟁사가 덜 몰린 넓은 구간'의 중심을 고름
    for wd, label in ((0.15, "⑧ 넓게 평활 0.15%p"), (0.3, "⑨ 넓게 평활 0.30%p")):
        out[label] = _pick_from_curve(win_curve(tl[0][1], width=wd))
    out["⑦ 현재 방식 + 예가범위 맞춤"] = _pick_from_curve(blend(
        [(n, same_range(h, row, 5 if n.startswith("같은 기관") else 20)) for n, h in tl])[0])
    out["⑤ 단순 평균 (사정율 평균)"] = round(float(tl[0][1]["sajung"].mean()), 2)
    w = tl[0][1]["winner_sajung"].dropna()
    if len(w):
        out["⑥ 과거 1순위 사정율 평균"] = round(float(w.mean()), 2)
    return out


def backtest(df: pd.DataFrame, keywords, test_days: int = 90, max_tests: int = 300,
             min_history: int = 50, progress=None, eligible_only: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """최근 test_days 일 동안의 공고 각각에 대해, 그 공고 '이전' 데이터만으로 사정율을 골랐다면
    실제로 1순위였는지 확인. (미래 데이터를 쓰지 않는 공정한 시험)"""
    done = df[df["sajung"].notna() & df["winner_sajung"].notna() & df["open_date"].notna()]
    done = done[done["winner_sajung"] >= done["sajung"]].sort_values("open_date")
    tests = apply_filters(done, keywords=keywords, eligible_only=eligible_only)
    tests = tests[tests["open_date"] >= pd.Timestamp.now() - pd.Timedelta(days=test_days)]
    tests = tests[~lowest_price(tests)]   # 최저가 입찰은 사정율로 이기는 게 아니라서 시험에서 뺌
    if len(tests) > max_tests:
        tests = tests.sample(max_tests, random_state=0).sort_values("open_date")
    # 개찰순위(상위 업체 투찰금액)가 있으면 '가상 순위'까지 계산
    rk = db.query_df("SELECT bid_key, amount FROM ranks WHERE rank > 0 AND amount IS NOT NULL")
    amounts = {k: np.sort(g["amount"].values) for k, g in rk.groupby("bid_key")} if len(rk) else {}
    # 업종·지역 표시를 전체 기간에 대해 한 번만 계산 (시험 공고마다 15만 건을 다시 거르지 않도록)
    kw_all = tuple(keywords or ())
    region = db.company()["region"]
    pre = df[df["sajung"].notna() | df["bid_key"].isin(tests["bid_key"])].copy()
    if kw_all:
        pre["_ind"] = pre["bid_key"].isin(apply_filters(pre, keywords=list(kw_all))["bid_key"])
        pre.attrs["_ind_key"] = kw_all
    if region:
        pre["_loc"] = pre["bid_key"].isin(local_region_history(pre, region)["bid_key"])
        pre.attrs["_loc_key"] = region
    keep = [c for c in ("bid_key", "name", "org", "open_date", "sajung", "winner_sajung", "participants",
                        "base_amt", "first_won", "_ind", "_loc", "range_begin", "range_end", "allowed_regions", "contract_method", "method", "est_price") if c in pre]
    attrs = dict(pre.attrs)
    pre = pre[keep].sort_values("open_date")
    pre.attrs.update(attrs)
    rows = []
    for i, (_, r) in enumerate(tests.iterrows()):
        if progress:
            progress(i + 1, len(tests))
        hist = pre[pre["open_date"] < r["open_date"]]
        hist.attrs.update(pre.attrs)
        if hist["sajung"].notna().sum() < min_history:
            continue
        kw_r = bid_keywords(r)
        kw_use = kw_r if tuple(kw_r) != kw_all or not kw_all else list(kw_all)
        picks = strategy_picks(hist, r, kw_use)
        try:
            _COMP_CACHE.clear()
            exp_p = expected_competition(hist, r)   # 그 공고 이전 자료로 예측한 참가업체 수
        except Exception:  # noqa: BLE001
            exp_p = None
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
                         "무작위 기대": (1 / p) if pd.notna(p) and p > 0 else np.nan,
                         "예상 참가": exp_p, "실제 참가": p})
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



def calc_check(df: pd.DataFrame) -> list[str]:
    """계산 점검 (실행 기록용): 사정율·낙찰자 사정율이 상식적인 범위인지."""
    done = apply_filters(df[df["sajung"].notna()], keywords=company_keywords(), eligible_only=True)
    if done.empty:
        return ["계산 점검: 개찰 완료 공고 없음"]
    w = done[done["winner_sajung"].notna()]
    gap = (w["winner_sajung"] - w["sajung"])
    pp = done["participants"].dropna()
    out = [f"계산 점검(우리 참가 가능·개찰 완료 {len(done):,}건, 기간 {done['open_date'].min():%Y-%m-%d}~)",
           f"  실제 사정율: 중앙 {done['sajung'].median():.3f}% · 98~102% 밖 {(~done['sajung'].between(98, 102)).mean() * 100:.1f}%",
           f"  낙찰자 사정율 있음 {len(w) / len(done) * 100:.0f}% · 낙찰자<실제(이상) {(gap < 0).mean() * 100:.1f}% · "
           f"낙찰 구간 폭 중앙 {gap[gap >= 0].median():.3f}%p · 1%p 넘음 {(gap > 1).mean() * 100:.1f}%",
           f"  참가업체 중앙 {pp.median():.0f}곳 · 10곳 미만 {(pp < 10).mean() * 100:.0f}%"]
    loc = local_region_history(done, db.company()["region"] or "울산")
    if len(loc):
        lw = loc[loc["winner_sajung"].notna()]
        out.append(f"  울산 지역제한 {len(loc):,}건 · 참가 중앙 {loc['participants'].median():.0f}곳 · "
                   f"낙찰 구간 폭 중앙 {(lw['winner_sajung'] - lw['sajung']).clip(lower=0).median():.3f}%p")
    # 최근 30일 우리 참가 가능 공고 중 사정율이 없는 공고는 어떤 공고인지
    now = pd.Timestamp.now()
    rec = apply_filters(df[(df["open_date"] < now) & (df["open_date"] >= now - pd.Timedelta(days=30))],
                        keywords=company_keywords(), eligible_only=True)
    miss = rec[rec["sajung"].isna()]
    if len(miss):
        amt = miss["base_amt"].fillna(miss["est_price"])
        band = pd.cut(amt, [0, 2e7, 5e7, 1e8, 3e8, 1e12], labels=["2천만↓", "2천~5천", "5천~1억", "1~3억", "3억↑"])
        meth = (miss["contract_method"].fillna("") + "/" + miss["method"].fillna("")).value_counts().head(4)
        out.append(f"  최근30일 사정율 없음 {len(miss)}/{len(rec)}건 · 기초금액 없음 {miss['base_amt'].isna().sum()} · "
                   f"개찰결과 있음 {miss['first_amt'].notna().sum()} · 금액대 "
                   + ", ".join(f"{k} {v}" for k, v in band.value_counts(sort=False).items()))
        out.append("  계약방법: " + ", ".join(f"{k} {v}" for k, v in meth.items()))
    if "plan_source" in done:
        vc = done["plan_source"].value_counts()
        out.append("  예정가격 출처: " + ", ".join(f"{k or '없음'} {v:,}" for k, v in vc.items()))
        both = done[(done["plan_source"] == "예비가격") & done["win_rate"].between(50, 110) & done["win_amt"].notna()]
        if len(both):
            dev = ((both["win_amt"] / (both["win_rate"] / 100)) / both["plan_price"] - 1).abs() * 100
            out.append(f"  낙찰률 역산 검증 {len(both):,}건: 오차 중앙 {dev.median():.4f}% · 0.01% 넘음 {(dev > 0.01).mean() * 100:.1f}%")
    by_m = done.assign(m=done["open_date"].dt.strftime("%Y-%m")).groupby("m")["sajung"].agg(["size", "median"])
    out.append("  월별 건수·사정율 중앙: " + ", ".join(f"{m} {int(r['size'])}건 {r['median']:.2f}" for m, r in by_m.iterrows()))
    return out


def data_health(df: pd.DataFrame, days: int = 30) -> pd.DataFrame:
    """최근 N일 개찰 공고 기준 데이터 연결 상태 (문제 진단용)."""
    if df.empty:
        return pd.DataFrame()
    now = pd.Timestamp.now()
    d = df[(df["open_date"] < now) & (df["open_date"] >= now - pd.Timedelta(days=days))]
    if d.empty:
        return pd.DataFrame()
    kw = company_keywords()
    mine = apply_filters(d, keywords=kw, eligible_only=True)
    rk = set(db.query_df("SELECT DISTINCT bid_key FROM ranks")["bid_key"])

    def pct(x, sub):
        return f"{x.sum() / len(sub) * 100:.0f}%" if len(sub) else "-"
    rows = []
    for label, sub in (("전체 공사 공고", d), ("우리 참가 가능 공고", mine)):
        rows.append({
            "구분": label, "공고 수": len(sub),
            "기초금액": pct(sub["base_amt"].notna(), sub),
            "예정가격(사정율)": pct(sub["sajung"].notna(), sub),
            "개찰결과(1순위)": pct(sub["first_amt"].notna(), sub),
            "최종 낙찰자": pct(sub["win_amt"].notna(), sub),
            "참가지역 확인": pct(sub["allowed_regions_known"], sub),
            "개찰순위(경쟁업체)": pct(sub["bid_key"].isin(rk), sub),
        })
    return pd.DataFrame(rows)
