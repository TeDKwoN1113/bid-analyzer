"""입찰 분석 프로그램 (화면). 실행: streamlit run app.py"""
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import importlib
import sys
import types

import analysis
import api_client
import collector
import config
import db
import sample_data


# 서버 프로세스가 살아 있는 동안 유지되는 저장소 (app.py 는 매번 새로 실행되므로 sys.modules 에 둠)
_STAMPS = sys.modules.setdefault("_bid_stamps", types.ModuleType("_bid_stamps")).__dict__.setdefault("v", {})


def _reload_changed_modules():
    """코드가 업데이트되면 다른 .py 파일도 다시 읽음.

    서버는 app.py 만 매번 새로 읽고 다른 .py 는 처음 읽은 것을 계속 쓰기 때문에,
    업데이트 후 '없는 함수' 오류가 날 수 있다. 처음 한 번은 전부, 이후에는 바뀐 것부터 의존 순서대로 다시 읽음.
    """
    first = "_init" not in _STAMPS
    changed = first
    for name in ("config", "db", "api_client", "analysis", "collector", "sample_data", "notify", "tune"):
        m = sys.modules.get(name)
        if m is None or not getattr(m, "__file__", None):
            continue
        mt = os.path.getmtime(m.__file__)
        if changed or _STAMPS.get(name, mt) != mt:
            importlib.reload(m)
            changed = True
        _STAMPS[name] = mt
    _STAMPS["_init"] = True


_reload_changed_modules()
from config import OPERATIONS, SAMPLE_DB_PATH, load_settings, save_settings  # noqa: E402

st.set_page_config(page_title="입찰 분석기", page_icon="⚡", layout="wide")

settings = load_settings()

# ---------------- 로그인 (외부 접속 시 필수) ----------------
_pw = settings.get("app_password") or os.environ.get("APP_PASSWORD", "")
if _pw and not st.session_state.get("authed"):
    st.title("⚡ 입찰 분석기")
    entered = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        if entered == _pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    st.stop()

# ---------------- 사이드바 ----------------
st.sidebar.title("⚡ 입찰 분석기")
page = st.sidebar.radio("메뉴", ["🏠 오늘의 투찰", "📋 공고 현황", "📊 사정율 분석", "🧮 투찰 계산기", "📝 우리 투찰",
                               "🏢 경쟁업체", "🔬 추천 검증", "⬇️ 데이터 수집", "⚙️ 설정 · API 점검"])

use_sample = st.sidebar.toggle("샘플(연습용) 데이터로 보기", value=not settings["service_key"],
                               help="API 연결 전에 기능을 미리 써보는 가상 데이터입니다.")
db.use_sample(use_sample)
if use_sample:
    if not os.path.exists(SAMPLE_DB_PATH):
        with st.spinner("샘플 데이터 만드는 중..."):
            sample_data.build()
            db.use_sample(True)
    st.sidebar.warning("지금은 **가상 데이터**입니다. 실제 투찰에 쓰지 마세요.")

st.sidebar.markdown("---")
st.sidebar.subheader("필터")
_company = db.company()
_lics = _company["licenses"]
sel_lics = st.sidebar.multiselect("업종 (보유 면허)", _lics, default=_lics,
                                  help="면허는 '⚙️ 설정 → 우리 회사'에서 추가합니다.")
kw_extra = st.sidebar.text_input("추가 키워드 (선택, 쉼표 구분)", "")
keywords = analysis.company_keywords(sel_lics) if sel_lics else []
keywords += [k.strip() for k in kw_extra.split(",") if k.strip() and k.strip() not in keywords]
company = db.company()
only_ok = st.sidebar.checkbox(
    f"우리가 참가 가능한 공고만 ({company['region']} 업체 · {', '.join(company['licenses'])})",
    value=True, help="참가가능지역에 우리 지역이 있거나 지역제한이 없는 공고, 우리 면허로 넣을 수 있는 공고만 봅니다.")
rg = st.sidebar.text_input("지역/기관 키워드(비우면 전체)", ", ".join(settings["default_regions"]),
                           help="공사현장 지역·기관명으로 더 좁힐 때 사용 (예: 울산)")
regions = [r.strip() for r in rg.split(",") if r.strip()]
amt_range = st.sidebar.slider("기초금액 (억원)", 0.0, 100.0,
                              (0.0, settings["max_amount"] / 1e8), step=0.5)
period = st.sidebar.selectbox("기간", ["최근 3개월", "최근 6개월", "최근 1년", "최근 2년", "전체"], index=2)
days = {"최근 3개월": 90, "최근 6개월": 180, "최근 1년": 365, "최근 2년": 730}.get(period)


@st.cache_data(ttl=600)
def _load(sample: bool):
    db.use_sample(sample)
    return analysis.load_dataset()


raw = _load(use_sample)
df = analysis.apply_filters(
    raw, keywords=keywords, regions=regions,
    min_amt=amt_range[0] * 1e8 or None, max_amt=amt_range[1] * 1e8,
    date_from=(date.today() - timedelta(days=days)) if days else None,
    eligible_only=only_ok,
)


def won(x):
    return "-" if pd.isna(x) else f"{x:,.0f}원"


def a_value_text(r) -> str:
    """A값 적용 여부와 구성 항목."""
    import json as _json
    if r.get("a_apply") == 0:
        return "A값 미적용 공고"
    try:
        d = _json.loads(r["a_detail"]) if isinstance(r.get("a_detail"), str) else {}
    except ValueError:
        d = {}
    if not d:
        return "A값 없음" if not r.get("a_value") else f"A값 {won(r['a_value'])}"
    return f"A값 {won(sum(d.values()))} = " + " + ".join(f"{k} {v:,.0f}" for k, v in d.items())


def need_data():
    if raw.empty:
        st.info("아직 데이터가 없습니다. '⬇️ 데이터 수집' 메뉴에서 먼저 수집하거나, "
                "사이드바에서 '샘플 데이터로 보기'를 켜세요.")
        st.stop()


def win_chart(hist: pd.DataFrame, my_s: float | None = None, recs=None):
    """사정율별 과거 낙찰확률 차트."""
    c = analysis.win_curve(hist)
    fig = go.Figure()
    fig.add_scatter(x=c["사정율"], y=c["낙찰확률(원값)"], name="원값", line=dict(color="#c9d6ea", width=1),
                    hoverinfo="skip")
    fig.add_scatter(x=c["사정율"], y=c["낙찰확률"], name="낙찰확률(%, ±0.05 평균)", fill="tozeroy",
                    line=dict(color="#1f6feb", width=2.5))
    fig.add_scatter(x=c["사정율"], y=c["하한통과확률"], name="하한 통과확률(%)", yaxis="y2",
                    line=dict(color="#9aa4b2", dash="dot"))
    for r in recs or []:
        fig.add_vline(x=r["추천 사정율"], line_color="#2da44e", line_width=1)
    if my_s is not None:
        fig.add_vline(x=my_s, line_dash="dash", line_color="#cf222e", annotation_text="내 사정율")
    fig.update_layout(xaxis_title="투찰 사정율(%)", yaxis=dict(title="낙찰확률(%)", rangemode="tozero"),
                      yaxis2=dict(title="하한 통과확률(%)", overlaying="y", side="right", range=[0, 100]),
                      height=420, legend=dict(orientation="h", y=1.12), margin=dict(t=40))
    st.plotly_chart(fig, use_container_width=True)
    return c


def random_baseline(hist: pd.DataFrame) -> float | None:
    """무작위로 넣었을 때 기대 낙찰확률 = 평균(1/참가업체수)."""
    p = hist["participants"].dropna()
    p = p[p > 0]
    return float((1 / p).mean() * 100) if len(p) else None


WIN_HELP = ("낙찰확률: 과거 비슷한 공고들에 이 사정율로 넣었다면 1순위였을 비율. "
            "(그 공고의 실제 사정율 이상이고, 실제 1순위가 노린 사정율보다 낮아야 1순위) "
            "※ 과거 통계이며 낙찰을 보장하지 않습니다. 적격심사 통과는 별도입니다.")


# ---------------- 오늘의 투찰 ----------------
if page == "🏠 오늘의 투찰":
    st.header("🏠 오늘의 투찰")
    need_data()
    now = pd.Timestamp.now()
    elig = analysis.apply_filters(raw, keywords=keywords, regions=regions, max_amt=amt_range[1] * 1e8,
                                  min_amt=amt_range[0] * 1e8 or None, eligible_only=only_ok)
    elig = elig.assign(close_date=pd.to_datetime(elig["close_dt"], errors="coerce", format="mixed"))
    todo = elig[(elig["open_date"] >= now) & elig["plan_price"].isna()].copy()
    todo["마감기준"] = todo["close_date"].fillna(todo["open_date"])
    todo = todo[todo["마감기준"] >= now].sort_values("마감기준")
    recent = elig[(elig["open_date"] < now) & (elig["open_date"] >= now - pd.Timedelta(days=3))]
    recent = recent[recent["plan_price"].notna() | recent["first_company"].notna()]
    mine = db.query_df("SELECT bid_key, amount FROM my_bids")
    mine_map = dict(zip(mine["bid_key"].str.rsplit("-", n=1).str[0], mine["amount"]))  # 공고번호 기준

    st.markdown(f"투찰 가능 **{len(todo)}건** · 3일 내 마감 "
                f"**{(todo['마감기준'] <= now + pd.Timedelta(days=3)).sum()}건** · "
                f"기초금액 공개 **{todo['base_amt_notice'].notna().sum()}건** · 최근 3일 개찰 **{len(recent)}건**")

    only_base = st.toggle("기초금액 공개된 공고만", value=False)
    if only_base:
        todo = todo[todo["base_amt_notice"].notna()]
    if todo.empty:
        st.info("조건에 맞는 개찰 전 공고가 없습니다. 왼쪽 필터를 넓혀보세요.")

    def dday(t):
        if pd.isna(t):
            return "-"
        d = (t.normalize() - now.normalize()).days
        return "오늘 마감" if d == 0 else f"D-{d}"

    # 기초금액 공개 공고는 미리 투찰금액·낙찰확률 계산 (정렬용)
    picks = {}
    for _, r in todo.head(80).iterrows():
        if pd.notna(r["base_amt_notice"]) and pd.notna(r["lower_rate"]):
            picks[r["bid_key"]] = analysis.pick_bid(raw, r, analysis.bid_keywords(r, sel_lics or _lics))
    order = st.radio("정렬", ["마감 임박순", "낙찰확률 높은순"], horizontal=True, label_visibility="collapsed")
    if order == "낙찰확률 높은순":
        todo = todo.assign(_p=todo["bid_key"].map(lambda k: (picks.get(k) or {}).get("낙찰확률") or -1))
        todo = todo.sort_values(["_p", "마감기준"], ascending=[False, True])
        st.caption("기초금액이 공개되어 투찰금액을 계산할 수 있는 공고부터, 과거 낙찰확률이 높은 순서입니다. "
                   "참가업체가 적은 공고일수록 확률이 높습니다.")

    for _, r in todo.head(40).iterrows():
        with st.container(border=True):
            top = st.columns([5, 1])
            link = f"[{r['name']}]({r['url']})" if isinstance(r["url"], str) and r["url"].startswith("http") else r["name"]
            top[0].markdown(f"**{link}**")
            top[1].markdown(f"**{dday(r['마감기준'])}**")
            limit = r["allowed_regions"] or ("제한없음" if r["allowed_regions_known"] else "확인중")
            st.caption(f"{r['org']} · 참가지역 {limit} · 투찰마감 "
                       f"{'-' if pd.isna(r['close_date']) else r['close_date'].strftime('%m/%d %H:%M')} · "
                       f"개찰 {r['open_date'].strftime('%m/%d %H:%M')} · {r['contract_method'] or r['method'] or ''}")
            qd = pd.to_datetime(r.get("qual_dt"), errors="coerce", format="mixed")
            if pd.notna(qd) and qd >= now:
                left = (qd.normalize() - now.normalize()).days
                txt = f"참가신청 마감 {qd.strftime('%m/%d %H:%M')}"
                if left <= 1:
                    st.markdown(f":red[**📝 {txt} ({'오늘' if left == 0 else '내일'})** — 아직 안 했다면 나라장터에서 참가신청하세요]")
                else:
                    st.caption(f"📝 {txt} (D-{left})")
            docs = []
            if isinstance(r.get("spec_docs"), str):
                try:
                    import json as _json
                    docs = _json.loads(r["spec_docs"])
                except ValueError:
                    docs = []
            if docs:
                st.markdown("📎 공고문: " + " · ".join(f"[{n[:24]}]({u})" for n, u in docs[:4]))
            if pd.isna(r["base_amt_notice"]):
                st.write(f"추정가격 {won(r['est_price'])} · 기초금액 **미공개** (공개되면 텔레그램으로 알려드립니다)")
                continue
            a_val = r["a_value"] or 0
            st.write(f"기초금액 **{won(r['base_amt'])}** · 하한율 "
                     f"{'-' if pd.isna(r['lower_rate']) else f'{r.lower_rate:.3f}%'}"
                     f"{'' if not a_val else ' · A값 ' + won(a_val)}")
            pc_min = analysis.pure_cost_min(r)
            st.caption(a_value_text(r) + (f" · 순공사원가 98% 하한 {pc_min:,}원" if pc_min else ""))
            if pd.isna(r["lower_rate"]):
                st.warning("하한율 정보가 없어 투찰금액을 계산할 수 없습니다. 공고문을 확인하세요.")
                continue
            pk = picks.get(r["bid_key"]) or analysis.pick_bid(raw, r, analysis.bid_keywords(r, sel_lics or _lics))
            if not pk:
                st.caption("투찰금액을 계산할 수 없습니다.")
                continue
            badge = {"높음": "🟢", "보통": "🟡", "낮음": "🔴"}[pk["신뢰도"]]
            st.metric("💰 투찰금액", f"{pk['투찰금액']:,}원")
            extra = []
            if pk.get("낙찰확률") is not None:
                extra.append(f"낙찰확률 {pk['낙찰확률']}%")
            if pk.get("예상 참가"):
                extra.append(f"예상 참가 약 {pk['예상 참가']:,}곳")
            st.caption(f"사정율 {pk['사정율']:.2f}% · 신뢰도 {badge} {pk['신뢰도']}"
                       + ("".join(" · " + x for x in extra)))
            qok, qmsg = pk.get("적격심사") or (None, "")
            if qok is False:
                st.warning("⚠️ " + qmsg)
            st.markdown("**📌 근거**  \n" + "  \n".join("• " + x for x in analysis.evidence_lines(pk)))
            ex = pk["근거상세"]["examples"]
            if ex:
                st.markdown("최근 비슷한 공고에 이 사정율로 넣었다면  \n" + "  \n".join(
                    f"– {x['공고명'][:28]} ({x['개찰일']}): 실제 {x['실제']:.2f} / 낙찰자 {x['1순위']:.2f} → **{x['결과']}**"
                    for x in ex))
            with st.expander("다른 후보 · 신뢰도 기준"):
                for s_, a_, p_ in pk["후보"][1:]:
                    st.write(f"- {s_:.2f}% → {a_:,}원 (과거 낙찰확률 {p_}%)")
                st.caption("낙찰 조건: 실제 사정율 ≤ 내 사정율 < 1순위 사정율. "
                           "신뢰도: 비교 공고 100건 이상 + 무작위의 2배 이상 = 높음, 30건 이상 = 보통, 그 외 낮음. "
                           "더 자세히는 '🧮 투찰 계산기'에서 그래프로 볼 수 있습니다.")
            if r["bid_no"] in mine_map:
                st.success(f"✓ 투찰 기록됨: {mine_map[r['bid_no']]:,.0f}원")
            else:
                if st.button("📝 이 금액으로 투찰 기록", key=f"save_{r['bid_key']}"):
                    db.upsert_many("my_bids", [{"bid_key": r["bid_key"], "amount": pk["투찰금액"],
                                                "sajung": pk["사정율"], "memo": "오늘의 투찰에서 저장", "my_rank": None,
                                                "checked_at": None, "created_at": db.now()}])
                    st.rerun()
    if len(todo) > 40:
        st.caption(f"마감이 가까운 40건만 표시했습니다. 전체는 '📋 공고 현황'에서 보세요. (총 {len(todo)}건)")

    st.subheader("📊 최근 3일 개찰 결과")
    if recent.empty:
        st.caption("없음")
    else:
        st.dataframe(recent.sort_values("open_date", ascending=False)[
            ["name", "org", "base_amt", "sajung", "first_company", "winner_sajung", "participants"]].rename(columns={
                "name": "공고명", "org": "기관", "base_amt": "기초금액", "sajung": "실제 사정율",
                "first_company": "1순위", "winner_sajung": "1순위 사정율", "participants": "참가수"}),
            hide_index=True, use_container_width=True,
            column_config={"기초금액": st.column_config.NumberColumn(format="%d"),
                           "실제 사정율": st.column_config.NumberColumn(format="%.3f"),
                           "1순위 사정율": st.column_config.NumberColumn(format="%.3f")})

# ---------------- 공고 현황 ----------------
elif page == "📋 공고 현황":
    st.header("📋 공고 현황")
    need_data()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("조건에 맞는 공고", f"{len(df):,}건")
    c2.metric("개찰 완료(예정가격 있음)", f"{df['sajung'].notna().sum():,}건")
    c3.metric("평균 사정율", f"{df['sajung'].mean():.3f}%" if df["sajung"].notna().any() else "-")
    c4.metric("평균 참가업체", f"{df['participants'].mean():,.0f}개" if df["participants"].notna().any() else "-")

    q = st.text_input("공고명 검색")
    view = analysis.apply_filters(df, name_query=q) if q else df
    now = pd.Timestamp.now()
    upcoming = view[view["plan_price"].isna() & (view["open_date"] >= now)].sort_values("open_date")
    st.subheader(f"개찰 전 공고 ({len(upcoming)}건)")
    up = upcoming.assign(참가지역=upcoming["allowed_regions"].where(upcoming["allowed_regions"] != "",
                                                              upcoming["allowed_regions_known"].map({True: "제한없음", False: "확인중"})))
    st.dataframe(up[["bid_key", "name", "org", "참가지역", "licenses", "base_amt", "lower_rate", "open_dt", "url"]]
                 .rename(columns={"bid_key": "공고번호", "name": "공고명", "org": "공고기관", "licenses": "면허제한",
                                  "base_amt": "기초금액", "lower_rate": "하한율", "open_dt": "개찰일시",
                                  "url": "링크"}),
                 use_container_width=True, hide_index=True,
                 column_config={"기초금액": st.column_config.NumberColumn(format="%d"),
                                "링크": st.column_config.LinkColumn(display_text="열기")})
    st.subheader("개찰 결과")
    done = view[view["plan_price"].notna() | (view["open_date"] < now)].sort_values("open_date", ascending=False)
    st.dataframe(done[["bid_key", "name", "org", "base_amt", "plan_price", "sajung", "participants",
                       "first_company", "first_amt", "winner_sajung", "open_dt"]]
                 .rename(columns={"bid_key": "공고번호", "name": "공고명", "org": "공고기관",
                                  "base_amt": "기초금액", "plan_price": "예정가격", "sajung": "사정율(%)",
                                  "participants": "참가수", "first_company": "1순위", "first_amt": "1순위 금액",
                                  "winner_sajung": "1순위 사정율(%)", "open_dt": "개찰일시"}),
                 use_container_width=True, hide_index=True,
                 column_config={"기초금액": st.column_config.NumberColumn(format="%d"),
                                "예정가격": st.column_config.NumberColumn(format="%d"),
                                "1순위 금액": st.column_config.NumberColumn(format="%d"),
                                "사정율(%)": st.column_config.NumberColumn(format="%.4f"),
                                "1순위 사정율(%)": st.column_config.NumberColumn(format="%.4f")})

# ---------------- 사정율 분석 ----------------
elif page == "📊 사정율 분석":
    st.header("📊 사정율 분석")
    st.caption("사정율 = 예정가격 ÷ 기초금액 × 100.  1순위 사정율 = 낙찰자가 써낸 금액을 하한율로 역산한 사정율.")
    need_data()
    orgs = df["org"].value_counts()
    sel = st.multiselect("발주기관 선택 (비우면 전체)", orgs.index.tolist(),
                         format_func=lambda o: f"{o} ({orgs[o]}건)")
    d = analysis.apply_filters(df, orgs=sel) if sel else df
    d = d[d["sajung"].notna()]
    if d.empty:
        st.warning("개찰 결과가 있는 공고가 없습니다.")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("분석 공고 수", f"{len(d):,}건")
    c2.metric("사정율 평균", f"{d['sajung'].mean():.3f}%")
    c3.metric("사정율 중앙값", f"{d['sajung'].median():.3f}%")
    ws = d["winner_sajung"].mean()
    c4.metric("1순위 사정율 평균", "-" if pd.isna(ws) else f"{ws:.3f}%")

    fig = go.Figure()
    fig.add_histogram(x=d["sajung"], xbins=dict(size=0.1), name="실제 사정율", opacity=0.6)
    fig.add_histogram(x=d["winner_sajung"], xbins=dict(size=0.1), name="1순위 사정율", opacity=0.6)
    fig.update_layout(barmode="overlay", xaxis_title="사정율(%)", yaxis_title="건수", height=420,
                      legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("🎯 사정율별 낙찰확률 (과거 공고 재현)")
    rec = analysis.recommend(d)
    if rec:
        base_p = random_baseline(d)
        st.dataframe(pd.DataFrame(rec), hide_index=True, use_container_width=True)
        if base_p:
            st.caption(f"참고: 아무 사정율이나 넣었을 때 기대 낙찰확률(1/참가업체수 평균) ≈ {base_p:.2f}%")
    else:
        st.info("1순위 데이터가 있는 공고가 5건 이상 있어야 계산합니다.")
    win_chart(d, recs=rec)
    st.caption(WIN_HELP)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("기관별 사정율")
        by = (d.groupby("org").agg(건수=("sajung", "size"), 평균=("sajung", "mean"),
                                   표준편차=("sajung", "std"), 일순위평균=("winner_sajung", "mean"))
              .query("건수 >= 3").sort_values("건수", ascending=False).round(4).reset_index()
              .rename(columns={"org": "기관", "일순위평균": "1순위 평균"}))
        st.dataframe(by, hide_index=True, use_container_width=True)
    with col2:
        st.subheader("월별 사정율 추이")
        m = d.set_index("open_date")["sajung"].resample("MS").mean().reset_index()
        st.plotly_chart(px.line(m, x="open_date", y="sajung", markers=True,
                                labels={"open_date": "월", "sajung": "평균 사정율(%)"}),
                        use_container_width=True)

    st.subheader("예비가격 번호별 추첨 빈도")
    dn = analysis.drawn_number_stats(d["bid_key"])
    if not dn.empty:
        st.plotly_chart(px.bar(dn, x="번호", y="추첨횟수"), use_container_width=True)
        st.caption("※ 번호 추첨은 원칙적으로 무작위입니다. 특정 번호 쏠림이 크지 않다면 참고만 하세요.")

# ---------------- 투찰 계산기 ----------------
elif page == "🧮 투찰 계산기":
    st.header("🧮 투찰 계산기")
    need_data()
    upcoming = df[df["plan_price"].isna() & (df["open_date"] >= pd.Timestamp.now())].sort_values("open_date")
    opts = ["(직접 입력)"] + [f"{r.bid_key} | {r.name}" for r in upcoming.itertuples()]
    pick = st.selectbox("개찰 전 공고 선택", opts)
    row = None
    if pick != "(직접 입력)":
        row = upcoming[upcoming["bid_key"] == pick.split(" | ")[0]].iloc[0]

    c1, c2, c3 = st.columns(3)
    base = c1.number_input("기초금액(원)", min_value=0.0, step=1000.0, format="%.0f",
                           value=float(row["base_amt"]) if row is not None and pd.notna(row["base_amt"]) else 1_000_000_000.0)
    lr = c2.number_input("낙찰하한율(%)", min_value=0.0, max_value=100.0, step=0.001, format="%.3f",
                         value=float(row["lower_rate"]) if row is not None and pd.notna(row["lower_rate"]) else 87.745)
    a_val = c3.number_input("A값(원, 없으면 0)", min_value=0.0, step=1000.0, format="%.0f",
                            value=float(row["a_value"]) if row is not None and pd.notna(row["a_value"]) else 0.0)
    st.caption("※ 하한율·A값은 반드시 공고문으로 다시 확인하세요.")
    if row is not None:
        st.caption(a_value_text(row))
    pc_min = analysis.pure_cost_min(row) if row is not None else None

    st.markdown("**비교할 과거 데이터 범위**")
    cc1, cc2 = st.columns(2)
    same_org = cc1.checkbox("같은 발주기관만", value=row is not None)
    band = cc2.slider("기초금액 ± 범위(%)", 0, 200, 100)
    hist = df[df["sajung"].notna()]
    if same_org and row is not None:
        hist = hist[hist["org"] == row["org"]]
    if band < 200 and base:
        hist = hist[(hist["base_amt"] >= base * (1 - band / 100)) & (hist["base_amt"] <= base * (1 + band / 100))]
    st.write(f"비교 대상: **{len(hist)}건**")
    if len(hist) < 10:
        st.warning("비교 데이터가 10건 미만이라 통계가 불안정합니다. 범위를 넓혀보세요.")

    rec = analysis.recommend(hist)
    base_p = random_baseline(hist)
    if rec:
        st.markdown("#### 🎯 추천 사정율 (과거 낙찰확률 기준)")
        cols = st.columns(len(rec))
        for col, r in zip(cols, rec):
            times = f" · 무작위의 {r['낙찰확률(%)'] / base_p:.1f}배" if base_p else ""
            col.metric(f"사정율 {r['추천 사정율']:.2f}%",
                       f"{analysis.bid_amount(base, r['추천 사정율'], lr, a_val):,}원")
            col.caption(f"낙찰확률 **{r['낙찰확률(%)']}%**{times}")
    elif len(hist):
        st.info("1순위 데이터가 있는 비교 공고가 5건 이상 있어야 추천합니다. 범위를 넓혀보세요.")

    default_s = rec[0]["추천 사정율"] if rec else 100.0
    my_s = st.number_input("내가 정한 사정율(%)", 95.0, 105.0, float(default_s), 0.01, format="%.3f")
    curve = analysis.win_curve(hist, round(my_s, 2) - 0.1, round(my_s, 2) + 0.1, 0.01)
    my_p = curve["낙찰확률"].iloc[len(curve) // 2] if len(curve) else None
    m1, m2 = st.columns(2)
    m1.metric("투찰금액", f"{analysis.bid_amount(base, my_s, lr, a_val):,}원")
    if pc_min and analysis.bid_amount(base, my_s, lr, a_val) < pc_min:
        st.error(f"⚠️ 순공사원가 98%({pc_min:,}원) 미만이라 낙찰에서 제외됩니다. "
                 f"사정율을 {analysis.min_sajung_for_amount(base, lr, a_val, pc_min):.2f}% 이상으로 올리세요.")
    m2.metric("이 사정율의 과거 낙찰확률", "-" if my_p is None or pd.isna(my_p) else f"{my_p:.2f}%")

    if row is not None:
        memo = st.text_input("메모 (선택)", key="calc_memo")
        if st.button("📝 이 금액으로 투찰 기록 저장"):
            db.upsert_many("my_bids", [{"bid_key": row["bid_key"], "amount": analysis.bid_amount(base, my_s, lr, a_val),
                                        "sajung": my_s, "memo": memo, "my_rank": None, "checked_at": None,
                                        "created_at": db.now()}])
            st.success("저장했습니다. 개찰 후 결과가 자동으로 채워집니다. ('📝 우리 투찰' 메뉴)")

    win_chart(hist, my_s, rec)
    st.caption(WIN_HELP)
    tbl = analysis.sajung_table(hist, base, lr, a_val)
    st.dataframe(tbl, hide_index=True, use_container_width=True, height=300,
                 column_config={"투찰금액(원)": st.column_config.NumberColumn(format="%d")})

# ---------------- 우리 투찰 ----------------
elif page == "📝 우리 투찰":
    st.header("📝 우리 회사 투찰 기록")
    if not company["bizno"]:
        st.info("'⚙️ 설정'에서 회사 사업자번호를 등록하면 개찰 후 우리 순위를 자동으로 찾아 채웁니다.")
    with st.expander("➕ 투찰 기록 추가", expanded=False):
        cand = raw.sort_values("open_date", ascending=False)
        cand = cand[cand["open_date"] >= pd.Timestamp.now() - pd.Timedelta(days=60)]
        opts = [f"{r.bid_key} | {r.name}" for r in cand.itertuples()]
        pick = st.selectbox("공고 (최근 60일 + 개찰 전)", opts) if opts else None
        c1, c2 = st.columns(2)
        amt_in = c1.number_input("투찰금액(원) (모르면 0)", min_value=0.0, step=1000.0, format="%.0f")
        s_in = c2.number_input("투찰 사정율(%) (금액을 넣었으면 비워도 됨)", min_value=0.0, max_value=110.0,
                               value=0.0, step=0.01, format="%.3f")
        memo = st.text_input("메모")
        if st.button("저장", type="primary") and pick:
            key = pick.split(" | ")[0]
            r = raw[raw["bid_key"] == key].iloc[0]
            amount = amt_in or (analysis.bid_amount(r["base_amt"], s_in, r["lower_rate"], r["a_value"])
                                if s_in and pd.notna(r["base_amt"]) and pd.notna(r["lower_rate"]) else None)
            db.upsert_many("my_bids", [{"bid_key": key, "amount": amount, "sajung": s_in or None, "memo": memo,
                                        "my_rank": None, "checked_at": None, "created_at": db.now()}])
            st.cache_data.clear()
            st.success("저장했습니다.")

    v = analysis.my_bids_view(raw)
    if v.empty:
        st.info("아직 기록이 없습니다. 투찰 계산기에서 '투찰 기록 저장'을 누르거나 위에서 추가하세요. "
                "사업자번호를 등록하면 개찰순위에 우리 회사가 나온 공고는 자동으로 추가됩니다.")
        st.stop()
    done = v[~v["결과"].isin(["개찰 전", "결과 대기"])]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("투찰 기록", f"{len(v)}건")
    c2.metric("1순위", f"{(v['결과'] == '🏆 1순위').sum()}건")
    c3.metric("하한 미달", f"{(v['결과'] == '하한 미달').sum()}건")
    gap = (done["sajung"] - done["sajung_bid"]).mean() if len(done) else None
    c4.metric("우리 사정율 - 실제 사정율 (평균)", "-" if gap is None or pd.isna(gap) else f"{gap:+.3f}%p",
              help="+ 면 평균적으로 예정가격보다 높게(안전하게), - 면 낮게(하한 미달 위험) 넣고 있다는 뜻")
    show = v.sort_values("open_date", ascending=False)[
        ["결과", "name", "org", "open_dt", "amount", "sajung", "sajung_bid", "winner_sajung", "my_rank",
         "participants", "first_company", "memo", "bid_key"]].rename(columns={
            "name": "공고명", "org": "기관", "open_dt": "개찰일시", "amount": "우리 금액", "sajung": "우리 사정율",
            "sajung_bid": "실제 사정율", "winner_sajung": "1순위 사정율", "my_rank": "우리 순위",
            "participants": "참가수", "first_company": "1순위 업체", "memo": "메모", "bid_key": "공고번호"})
    st.dataframe(show, hide_index=True, use_container_width=True,
                 column_config={"우리 금액": st.column_config.NumberColumn(format="%d"),
                                "우리 사정율": st.column_config.NumberColumn(format="%.3f"),
                                "실제 사정율": st.column_config.NumberColumn(format="%.3f"),
                                "1순위 사정율": st.column_config.NumberColumn(format="%.3f")})
    st.caption("낙찰 조건: 실제 사정율 ≤ 우리 사정율 < 1순위 사정율. 우리 사정율이 실제 사정율보다 낮으면 하한 미달, "
               "1순위 사정율보다 높으면 다른 업체가 더 낮게 넣은 것입니다.")
    if len(done):
        fig = go.Figure()
        d2 = done.sort_values("open_date")
        fig.add_scatter(x=d2["open_date"], y=d2["sajung_bid"], mode="markers", name="실제 사정율(하한 기준)",
                        marker=dict(symbol="line-ew-open", size=14, color="#9aa4b2"))
        fig.add_scatter(x=d2["open_date"], y=d2["winner_sajung"], mode="markers", name="1순위 사정율",
                        marker=dict(symbol="line-ew-open", size=14, color="#2da44e"))
        fig.add_scatter(x=d2["open_date"], y=d2["sajung"], mode="markers", name="우리 사정율",
                        marker=dict(size=9, color="#cf222e"))
        fig.update_layout(yaxis_title="사정율(%)", height=380, legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("빨간 점이 회색 선과 초록 선 사이에 있었으면 낙찰입니다.")
    with st.expander("기록 삭제"):
        dk = st.selectbox("삭제할 공고", v["bid_key"] + " | " + v["name"].fillna(""))
        if st.button("삭제"):
            db.execute("DELETE FROM my_bids WHERE bid_key = :k", {"k": dk.split(" | ")[0]})
            st.success("삭제했습니다. 새로고침하면 반영됩니다.")

# ---------------- 경쟁업체 ----------------
elif page == "🏢 경쟁업체":
    st.header("🏢 경쟁업체 투찰 경향")
    need_data()
    st.caption("관심 공고(설정의 업종·지역 키워드)의 개찰순위 상위 업체를 매일 자동 수집해 분석합니다. "
               "왼쪽 필터(업종·지역·금액·기간)가 적용됩니다.")
    ranks = analysis.load_ranks(df["bid_key"])
    if ranks.empty:
        st.info("아직 개찰순위 데이터가 없습니다. 자동 수집이 돌면 채워지고, '데이터 수집' 메뉴에서 바로 받을 수도 있습니다.")
        st.stop()
    c1, c2, c3 = st.columns(3)
    c1.metric("분석 공고", f"{ranks['bid_key'].nunique():,}건")
    c2.metric("등장 업체", f"{ranks['company'].nunique():,}곳")
    c3.metric("평균 투찰사정율(상위권)", f"{ranks['투찰사정율'].mean():.3f}%")

    min_cnt = st.slider("최소 참가 횟수", 1, 20, 2)
    cs = analysis.competitor_stats(ranks, min_cnt)
    name = st.text_input("업체명 검색")
    if name:
        cs = cs[cs["업체명"].fillna("").str.contains(name, regex=False)]
    st.dataframe(cs, hide_index=True, use_container_width=True, height=400,
                 column_config={"평균 투찰사정율": st.column_config.NumberColumn(format="%.3f"),
                                "사정율 편차": st.column_config.NumberColumn(format="%.3f")})
    st.caption("투찰사정율 = 업체가 써낸 금액을 하한율로 역산한 사정율(그 업체가 노린 사정율). "
               "편차가 작은 업체는 늘 비슷한 사정율로 넣는 '패턴형' 업체입니다.")

    if not cs.empty:
        st.subheader("업체별 투찰 사정율 분포")
        pick = st.selectbox("업체 선택", cs["업체명"].dropna().tolist())
        one = ranks[ranks["company"] == pick]
        fig = go.Figure()
        fig.add_histogram(x=ranks["투찰사정율"], xbins=dict(size=0.05), name="전체 상위권",
                          histnorm="percent", opacity=0.45)
        fig.add_histogram(x=one["투찰사정율"], xbins=dict(size=0.05), name=pick,
                          histnorm="percent", opacity=0.75)
        fig.update_layout(barmode="overlay", xaxis_title="투찰 사정율(%)", yaxis_title="비율(%)",
                          height=380, legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(one.sort_values("open_date", ascending=False)[
            ["open_dt", "name", "org", "rank", "amount", "투찰사정율"]].rename(columns={
                "open_dt": "개찰일시", "name": "공고명", "org": "기관", "rank": "순위",
                "amount": "투찰금액", "투찰사정율": "투찰사정율(%)"}),
            hide_index=True, use_container_width=True,
            column_config={"투찰금액": st.column_config.NumberColumn(format="%d"),
                           "투찰사정율(%)": st.column_config.NumberColumn(format="%.3f")})

# ---------------- 추천 검증 ----------------
elif page == "🔬 추천 검증":
    st.header("🔬 추천 방식 검증 (백테스트)")
    st.markdown(
        "지난 공고 하나하나에 대해 **그 공고 이전 데이터만으로** 사정율을 골랐다면 실제로 어땠는지 시험합니다. "
        "미래 데이터를 쓰지 않으므로, 실제로 이 방식대로 넣었을 때의 성적과 같습니다.")
    need_data()
    import json as _json
    cur = analysis.current_strategy()
    st.info(f"현재 투찰금액 계산 방식: **{analysis.STRATEGIES[cur]}**  \n"
            "매주 월요일 새벽 자동으로 검증해서, 확실히 더 좋은 방식이 있으면 바꿉니다. (텔레그램으로 결과 알림)")
    ev = db.get_kv("strategy_eval")
    if ev:
        try:
            e = _json.loads(ev)
            st.caption(f"마지막 자동 검증 {e['date']}: {e['reason']}")
            if e.get("summary"):
                with st.expander("마지막 자동 검증 결과"):
                    st.dataframe(pd.DataFrame(e["summary"]), hide_index=True, use_container_width=True)
        except (ValueError, KeyError):
            pass
    with st.expander("방식 직접 바꾸기"):
        pick_mode = st.selectbox("투찰금액 계산 방식", list(analysis.STRATEGIES),
                                 index=list(analysis.STRATEGIES).index(cur),
                                 format_func=lambda m: analysis.STRATEGIES[m])
        if st.button("이 방식으로 저장"):
            db.set_kv("strategy", pick_mode)
            st.cache_data.clear()
            st.success("저장했습니다. 오늘의 투찰·알림에 바로 적용됩니다.")
    c1, c2 = st.columns(2)
    days_bt = c1.selectbox("시험 기간", [30, 60, 90, 180], index=2, format_func=lambda d: f"최근 {d}일")
    n_bt = c2.selectbox("최대 시험 공고 수", [100, 200, 300, 500], index=1)
    test_df = analysis.apply_filters(raw, keywords=keywords, regions=regions, eligible_only=only_ok,
                                     max_amt=amt_range[1] * 1e8, min_amt=amt_range[0] * 1e8 or None)
    if st.button("검증 실행", type="primary"):
        bar = st.progress(0.0, text="시험 중...")
        summ, det = analysis.backtest(
            raw[raw["bid_key"].isin(test_df["bid_key"]) | (raw["open_date"] < pd.Timestamp.now() - pd.Timedelta(days=days_bt))],
            keywords, test_days=days_bt, max_tests=n_bt,
            progress=lambda i, n: bar.progress(i / max(n, 1), text=f"시험 중... {i}/{n}"))
        st.session_state["bt"] = (summ, det, days_bt)
    if "bt" in st.session_state:
        summ, det, d_ = st.session_state["bt"]
        if summ.empty:
            st.warning("시험할 공고가 부족합니다. 데이터가 더 쌓인 뒤 다시 실행하세요.")
        else:
            st.subheader(f"결과 (최근 {d_}일, 시험 공고 {int(summ['시험 공고'].max())}건)")
            st.dataframe(summ, hide_index=True, use_container_width=True)
            st.markdown(
                "- **1순위**: 그 방식대로 넣었으면 실제로 1순위였던 공고 수\n"
                "- **무작위 기대 1순위**: 아무렇게나 넣었을 때 기대되는 1순위 수 (참가업체 수로 계산)\n"
                "- **10위 이내 / 중앙 순위**: 개찰순위를 받은 공고에서, 그 금액이었다면 몇 위였을지. "
                "1순위는 워낙 드물어서 이 지표가 방식의 좋고 나쁨을 더 빨리 보여줍니다\n"
                "- **하한 미달률**: 너무 낮게 넣어 탈락했을 비율")
            st.caption("※ 시험 공고가 수백 건이어도 1순위는 몇 건 수준이라 우연의 영향이 큽니다. "
                       "10위 이내율과 중앙 순위를 함께 보세요. 데이터가 쌓일수록 결과가 믿을 만해집니다.")
            with st.expander("공고별 상세"):
                st.dataframe(det.drop(columns=["순위 있음"]).sort_values(["개찰일", "방식"], ascending=[False, True]),
                             hide_index=True, use_container_width=True,
                             column_config={"실제 사정율": st.column_config.NumberColumn(format="%.3f"),
                                            "1순위 사정율": st.column_config.NumberColumn(format="%.3f"),
                                            "무작위 기대": st.column_config.NumberColumn(format="%.4f")})

# ---------------- 데이터 수집 ----------------
elif page == "⬇️ 데이터 수집":
    st.header("⬇️ 나라장터 데이터 수집")
    if use_sample:
        st.warning("사이드바의 '샘플 데이터로 보기'를 끄고 수집하세요.")
        st.stop()
    if not settings["service_key"]:
        st.error("먼저 '⚙️ 설정' 메뉴에서 API 인증키를 입력하세요.")
        st.stop()
    if db.is_remote():
        st.success("데이터는 **영구 저장 DB**에 저장됩니다. 매일 새벽 자동 수집도 이 DB에 쌓입니다.")
    else:
        st.warning("영구 저장 DB가 연결되지 않았습니다. 웹사이트에서는 서버가 재시작되면 데이터가 지워질 수 있습니다.")
    mb = db.size_mb()
    if mb is not None:
        lim = settings["db_limit_mb"]
        st.progress(min(mb / lim, 1.0), text=f"저장 공간: {mb:,.0f}MB / {lim}MB ({mb / lim * 100:.0f}%)")
        with st.expander("테이블별 용량 · 정리"):
            st.dataframe(db.table_sizes(), hide_index=True, use_container_width=True)
            st.caption("자동 수집이 매일 우리 업종이 아닌 공고의 '예비가격 15개 상세'를 정리합니다. "
                       "(예정가격·사정율은 남아서 분석에 지장 없음)")
            if st.button("지금 정리하기"):
                n = db.prune_prelim(analysis.company_keywords())
                st.success(f"{n:,}행 정리했습니다.")
    st.subheader("🩺 데이터 점검 (최근 30일 개찰 공고)")
    hd = analysis.data_health(analysis.load_dataset())
    if hd.empty:
        st.caption("최근 30일 개찰 공고가 아직 없습니다.")
    else:
        st.dataframe(hd, hide_index=True, use_container_width=True)
        st.caption("각 정보가 연결된 공고 비율입니다. 기초금액·예정가격·개찰결과가 80% 이상이면 정상이며, "
                   "낮으면 이 표를 캡처해서 보내주세요. (최종 낙찰자는 적격심사 후 며칠 뒤에 채워집니다)")
    st.subheader("과거 데이터 채우기 현황")
    st.dataframe(pd.DataFrame(collector.backfill_status()), hide_index=True, use_container_width=True)
    st.caption("자동 수집이 매일 최근 3일 + 과거 데이터를 조금씩 채웁니다. 목표 날수에 도달하면 1년치 분석이 가능합니다.")
    st.markdown("---")
    st.subheader("직접 수집")
    st.markdown("공사 입찰공고 · 기초금액 · 개찰결과 · 예비가격 · 낙찰자를 날짜별로 받아 저장합니다. "
                "(공공데이터포털 개발계정은 하루 호출 횟수 제한이 있습니다.)")
    c1, c2 = st.columns(2)
    d1 = c1.date_input("시작일", date.today() - timedelta(days=30))
    d2 = c2.date_input("종료일", date.today())
    jobs = st.multiselect("수집 항목", [j[0] for j in collector.JOBS], [j[0] for j in collector.JOBS],
                          format_func=lambda k: collector.LABEL[k])
    if st.button("수집 시작", type="primary"):
        bar = st.progress(0.0)
        res = collector.collect(d1, d2, jobs=jobs, progress=lambda t, p: bar.progress(min(p, 1.0), text=t))
        st.cache_data.clear()
        for label, v in res.items():
            if v["stopped"]:
                st.error(f"{label}: {v['count']:,}건 저장 후 중단 → {v['stopped']}")
            elif v["errors"]:
                st.warning(f"{label}: {v['count']:,}건 저장, 오류 {len(v['errors'])}건 → {v['errors'][0]}")
            else:
                st.success(f"{label}: {v['count']:,}건 저장")

    st.markdown("---")
    st.subheader("관심 공고 개찰순위 한꺼번에 받기")
    st.caption("설정의 업종·지역 키워드에 맞고 아직 순위를 안 받은 공고를 최신순으로 받습니다. (매일 자동 수집에도 포함)")
    if st.button("관심 공고 개찰순위 받기"):
        bar2 = st.progress(0.0)
        r = collector.collect_ranks_auto(progress=lambda t, p: bar2.progress(min(p, 1.0), text=t))
        st.cache_data.clear()
        (st.warning if r["stopped"] or r["errors"] else st.success)(
            f"공고 {r['bids']}/{r['todo']}건, 업체 {r['rows']:,}행 저장 {r['stopped']}")

    st.subheader("공고별 개찰순위(업체별 투찰금액) 받기")
    bn = st.text_input("공고번호 (예: R26BK01234567)")
    bo = st.text_input("차수", "000")
    if st.button("개찰순위 받기") and bn:
        try:
            n = collector.collect_ranks(bn.strip(), bo.strip())
            st.success(f"{n}개 업체 저장")
        except api_client.ApiError as e:
            st.error(str(e))

    st.subheader("수집 기록")
    st.dataframe(db.query_df('SELECT run_at AS "시각", kind AS "항목", period AS "기간", count AS "건수", '
                             'message AS "메시지" FROM collect_log ORDER BY run_at DESC LIMIT 50'),
                 hide_index=True, use_container_width=True)

# ---------------- 설정 ----------------
elif page == "⚙️ 설정 · API 점검":
    st.header("⚙️ 설정")
    key = st.text_input("공공데이터포털 인증키 (Decoding 키 권장)", settings["service_key"], type="password")
    kws = st.text_input("기본 업종 키워드 (면허를 등록하지 않았을 때만 사용)", ", ".join(settings["target_keywords"]))
    rgs = st.text_input("기본 지역 키워드 (비우면 참가 가능한 공고 전체)", ", ".join(settings["default_regions"]))
    st.subheader("🏢 우리 회사")
    cc1, cc2 = st.columns(2)
    cname = cc1.text_input("회사명", company["name"])
    cbiz = cc2.text_input("사업자번호 (개찰순위에서 우리 순위 찾기용, 숫자 10자리)", company["bizno"])
    creg = cc1.text_input("소재지 (참가가능지역 판단, 예: 울산)", company["region"])
    clic = cc2.text_input("보유 면허 (쉼표 구분, 예: 전기공사업, 정보통신공사업)", ", ".join(company["licenses"]))
    qlim = st.text_input("적격심사 통과 가능한 최대 추정가격(억원, 비우면 판정 안 함)",
                         db.get_kv("qual_max_est", "") or "",
                         help="시공능력·실적·경영상태 등을 고려해 '이 금액까지는 수행능력 만점(심사 통과)'인 추정가격. "
                              "넘는 공고에는 경고가 붙습니다.")
    if st.button("회사 정보 저장"):
        db.set_kv("qual_max_est", qlim.strip())
        db.set_kv("company_name", cname.strip())
        db.set_kv("company_bizno", cbiz.strip().replace("-", ""))
        db.set_kv("company_region", creg.strip())
        db.set_kv("company_licenses", clic.strip())
        st.cache_data.clear()
        st.success("저장했습니다. (자동 수집·알림에도 적용됩니다)")
    with st.expander("🧮 적격심사 한도 계산 도우미"):
        st.caption("공고에 첨부된 적격심사 기준표의 값을 넣으면 '통과 가능한 최대 추정가격'을 계산합니다. "
                   "구간·기관마다 기준이 다르니, 가장 자주 넣는 기관의 기준표로 계산하세요.")
        def _f(k, d=0.0):
            try:
                return float(db.get_kv(k, d) or d)
            except ValueError:
                return d
        q1, q2, q3 = st.columns(3)
        perf = q1.number_input("최근 5년 동일업종 실적 합계(억원)", 0.0, 100000.0, _f("qual_perf"), 0.1)
        mult = q2.number_input("시공경험 만점 배수 (기준표, 예: 1배)", 0.1, 10.0, _f("qual_mult", 1.0), 0.1)
        mgmt = q3.number_input("경영상태 만점 가능한 최대 추정가격(억원, 모르면 0)", 0.0, 100000.0, _f("qual_mgmt"), 0.5)
        single = st.checkbox("단일공종 공사라 시공경험 만점 처리되는 구간 (실적 계산 생략)",
                             value=db.get_kv("qual_single", "0") == "1")
        limits = []
        if not single and perf > 0:
            base_lim = perf / mult                      # 시공경험 만점이 되는 최대 기초금액
            limits.append(("시공경험", base_lim / 1.1))  # 추정가격 ≈ 기초금액 ÷ 1.1
            st.write(f"- 시공경험: 실적 {perf:,.1f}억 ÷ 배수 {mult:g} = 기초금액 **{base_lim:,.1f}억**까지 만점 "
                     f"→ 추정가격 약 **{base_lim / 1.1:,.1f}억**")
        if mgmt > 0:
            limits.append(("경영상태", mgmt))
            st.write(f"- 경영상태: 추정가격 **{mgmt:,.1f}억**까지 만점")
        if limits:
            lim = min(v for _, v in limits)
            st.success(f"통과 가능 최대 추정가격 ≈ **{lim:,.1f}억원** (가장 작은 한도: "
                       f"{min(limits, key=lambda x: x[1])[0]})")
            if st.button("이 값으로 적용"):
                db.set_kv("qual_max_est", f"{lim:.1f}")
                for k, v in (("qual_perf", perf), ("qual_mult", mult), ("qual_mgmt", mgmt)):
                    db.set_kv(k, str(v))
                db.set_kv("qual_single", "1" if single else "0")
                st.cache_data.clear()
                st.success("적용했습니다. 이 금액을 넘는 공고에 경고가 붙습니다.")
        else:
            st.caption("실적 또는 경영상태 한도를 넣으면 계산됩니다.")
    st.subheader("분석 기본값")
    mx = st.number_input("기본 최대 금액(억원)", 1.0, 1000.0, settings["max_amount"] / 1e8)
    apw = st.text_input("접속 비밀번호 (외부/휴대폰 접속 시 꼭 설정)", settings.get("app_password", ""),
                        type="password")
    if st.button("저장", type="primary"):
        settings["app_password"] = apw.strip()
        settings["service_key"] = key.strip()
        settings["target_keywords"] = [k.strip() for k in kws.split(",") if k.strip()]
        settings["default_regions"] = [r.strip() for r in rgs.split(",") if r.strip()]
        settings["max_amount"] = int(mx * 1e8)
        save_settings(settings)
        st.success("저장했습니다. 사이드바의 '샘플 데이터로 보기'를 끄면 실제 데이터를 씁니다.")

    st.markdown("---")
    st.header("🔔 알림")
    import notify
    ch = notify.channels(settings)
    if ch:
        st.success(f"알림 채널 연결됨: {', '.join(ch)}")
    else:
        st.info("텔레그램 또는 이메일이 아직 설정되지 않았습니다. 사용설명서의 '알림 설정'을 참고해 "
                "Streamlit Secrets 와 GitHub Secrets 에 값을 넣어주세요.")
    kw_n = settings["notify_keywords"] or analysis.company_keywords()
    rg_n = settings["notify_regions"] or settings["default_regions"]
    mx_n = settings["notify_max_amount"] or settings["max_amount"]
    st.caption(f"알림 조건: 업종 {', '.join(kw_n)} · 지역 {', '.join(rg_n) or '전국'} · {mx_n / 1e8:,.0f}억 미만 "
               "(위의 기본 키워드/금액 설정을 따릅니다)")
    b1, b2, b3 = st.columns(3)
    if b1.button("텔레그램 chat_id 찾기"):
        if not settings.get("telegram_bot_token"):
            st.error("먼저 Secrets 에 TELEGRAM_BOT_TOKEN 을 넣어주세요.")
        else:
            try:
                chats = notify.telegram_chats(settings["telegram_bot_token"])
                if chats:
                    st.dataframe(pd.DataFrame(chats), hide_index=True)
                    st.caption("위 chat_id 숫자를 TELEGRAM_CHAT_ID 로 Secrets 에 넣으세요.")
                else:
                    st.warning("대화 기록이 없습니다. 텔레그램에서 내 봇을 열고 아무 메시지나 보낸 뒤 다시 누르세요.")
            except Exception as e:  # noqa: BLE001
                st.error(f"조회 실패: {e}")
    if b2.button("테스트 알림 보내기"):
        try:
            sent = notify.send("⚡ 입찰 분석기 테스트", "알림 연결이 정상입니다 ✅")
            st.success(f"보냄: {', '.join(sent)}") if sent else st.warning("설정된 채널이 없습니다.")
        except Exception as e:  # noqa: BLE001
            st.error(f"전송 실패: {e}")
    if b3.button("다음 알림 미리보기"):
        text, keys = notify.build()
        if text:
            st.caption(f"{len(keys)}건 (아직 보내지 않음)")
            st.markdown(text.replace("\n", "  \n"), unsafe_allow_html=True)
        else:
            st.info("보낼 새 소식이 없습니다.")

    st.markdown("---")
    st.header("🔌 API 점검")
    st.caption("API가 정상 응답하는지, 어떤 항목(필드)이 오는지 확인합니다. 문제가 생기면 이 화면 결과를 캡처해 주세요.")
    op = st.selectbox("API 종류", list(OPERATIONS.keys()),
                      format_func=lambda k: {"bid_list": "입찰공고", "regions": "참가가능지역", "licenses": "면허제한",
                                             "base_amount": "기초금액", "openings": "개찰결과",
                                             "winners": "낙찰자", "prelim_prices": "예비가격 상세",
                                             "ranks": "개찰순위"}[k])
    c1, c2, c3 = st.columns(3)
    div = c1.text_input("inqryDiv", settings["inqry_div_date"].get(op, "1"))
    day = c2.date_input("조회일", date.today() - timedelta(days=1))
    bidno = c3.text_input("공고번호(선택)")
    if st.button("호출해보기"):
        p = {"inqryDiv": div}
        if bidno:
            p["bidNtceNo"] = bidno
        else:
            p["inqryBgnDt"] = day.strftime("%Y%m%d") + "0000"
            p["inqryEndDt"] = day.strftime("%Y%m%d") + "2359"
        try:
            st.code(api_client.raw_call(op, p))
        except Exception as e:  # noqa: BLE001
            st.error(f"호출 실패: {e}")
    with st.expander("고급: 날짜 조회용 inqryDiv 값 변경"):
        for k in settings["inqry_div_date"]:
            settings["inqry_div_date"][k] = st.text_input(k, settings["inqry_div_date"][k], key=f"div_{k}")
        if st.button("inqryDiv 저장"):
            save_settings(settings)
            st.success("저장했습니다.")
