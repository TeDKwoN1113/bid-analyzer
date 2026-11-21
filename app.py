"""입찰 분석 프로그램 (화면). 실행: streamlit run app.py"""
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import analysis
import api_client
import collector
import db
import sample_data
from config import OPERATIONS, SAMPLE_DB_PATH, load_settings, save_settings

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
page = st.sidebar.radio("메뉴", ["📋 공고 현황", "📊 사정율 분석", "🧮 투찰 계산기",
                               "🏢 경쟁업체", "⬇️ 데이터 수집", "⚙️ 설정 · API 점검"])

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
kw = st.sidebar.text_input("업종 키워드(쉼표 구분)", ", ".join(settings["target_keywords"]))
keywords = [k.strip() for k in kw.split(",") if k.strip()]
rg = st.sidebar.text_input("지역/기관 키워드(비우면 전국)", ", ".join(settings["default_regions"]))
regions = [r.strip() for r in rg.split(",") if r.strip()]
amt_range = st.sidebar.slider("기초금액 (억원)", 0.0, 100.0,
                              (0.0, settings["max_amount"] / 1e8), step=0.5)
period = st.sidebar.selectbox("기간", ["최근 3개월", "최근 6개월", "최근 1년", "최근 2년", "전체"], index=2)
days = {"최근 3개월": 90, "최근 6개월": 180, "최근 1년": 365, "최근 2년": 730}.get(period)


@st.cache_data(ttl=60)
def _load(sample: bool):
    db.use_sample(sample)
    return analysis.load_dataset()


raw = _load(use_sample)
df = analysis.apply_filters(
    raw, keywords=keywords, regions=regions,
    min_amt=amt_range[0] * 1e8 or None, max_amt=amt_range[1] * 1e8,
    date_from=(date.today() - timedelta(days=days)) if days else None,
)


def won(x):
    return "-" if pd.isna(x) else f"{x:,.0f}원"


def need_data():
    if raw.empty:
        st.info("아직 데이터가 없습니다. '⬇️ 데이터 수집' 메뉴에서 먼저 수집하거나, "
                "사이드바에서 '샘플 데이터로 보기'를 켜세요.")
        st.stop()


# ---------------- 공고 현황 ----------------
if page == "📋 공고 현황":
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
    st.dataframe(upcoming[["bid_key", "name", "org", "region", "base_amt", "lower_rate", "open_dt", "url"]]
                 .rename(columns={"bid_key": "공고번호", "name": "공고명", "org": "공고기관", "region": "지역",
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

    st.subheader("추천 구간 (과거 1순위가 가장 많이 나온 사정율)")
    rec = analysis.recommend(d)
    if rec:
        st.dataframe(pd.DataFrame(rec), hide_index=True, use_container_width=True)
        st.caption("※ 과거 통계일 뿐, 예정가격은 추첨으로 정해지므로 낙찰을 보장하지 않습니다.")
    else:
        st.info("1순위 데이터가 5건 이상 있어야 추천합니다.")

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
    if rec:
        best = rec[0]["추천 사정율"]
        st.success(f"과거 1순위가 가장 많이 몰린 구간: **{rec[0]['구간']}** → "
                   f"이 사정율({best}%)로 투찰하면 **{analysis.bid_amount(base, best, lr, a_val):,}원**")

    my_s = st.number_input("내가 정한 사정율(%)", 95.0, 105.0, 100.0, 0.01, format="%.3f")
    st.metric("투찰금액", f"{analysis.bid_amount(base, my_s, lr, a_val):,}원")

    tbl = analysis.sajung_table(hist, base, lr, a_val)
    fig = go.Figure()
    fig.add_bar(x=tbl["사정율(%)"], y=tbl["과거 1순위 빈도(건)"], name="과거 1순위 빈도")
    fig.add_scatter(x=tbl["사정율(%)"], y=tbl["하한 통과확률(%)"], name="하한 통과확률(%)", yaxis="y2")
    fig.add_vline(x=my_s, line_dash="dash", annotation_text="내 사정율")
    fig.update_layout(yaxis=dict(title="건수"), yaxis2=dict(title="%", overlaying="y", side="right",
                                                          range=[0, 100]),
                      height=420, legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("하한 통과확률: 과거 예정가격 기준으로, 이 사정율로 썼다면 하한율 미달(탈락)이 아니었을 비율. "
               "낮은 사정율일수록 통과확률이 높지만 1순위가 되기는 어려워집니다.")
    st.dataframe(tbl, hide_index=True, use_container_width=True, height=300,
                 column_config={"투찰금액(원)": st.column_config.NumberColumn(format="%d")})

# ---------------- 경쟁업체 ----------------
elif page == "🏢 경쟁업체":
    st.header("🏢 경쟁업체 투찰 경향")
    need_data()
    st.caption("개찰순위(업체별 투찰금액)를 수집한 공고만 분석됩니다. '데이터 수집' 메뉴에서 공고별로 받을 수 있습니다.")
    cs = analysis.competitor_stats()
    if cs.empty:
        st.info("개찰순위 데이터가 없습니다.")
    else:
        name = st.text_input("업체명 검색")
        if name:
            cs = cs[cs["업체명"].str.contains(name, regex=False)]
        st.dataframe(cs, hide_index=True, use_container_width=True)

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
    kws = st.text_input("기본 업종 키워드", ", ".join(settings["target_keywords"]))
    rgs = st.text_input("기본 지역 키워드", ", ".join(settings["default_regions"]))
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
    st.header("🔌 API 점검")
    st.caption("API가 정상 응답하는지, 어떤 항목(필드)이 오는지 확인합니다. 문제가 생기면 이 화면 결과를 캡처해 주세요.")
    op = st.selectbox("API 종류", list(OPERATIONS.keys()),
                      format_func=lambda k: {"bid_list": "입찰공고", "base_amount": "기초금액", "openings": "개찰결과",
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
