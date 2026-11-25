"""입찰 분석 프로그램 (화면). 실행: streamlit run app.py"""
import os
import json
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

# 브라우저 자동 번역(크롬 '한국어로 번역' 등)이 화면 글자를 바꾸면 Streamlit 이
# "removeChild ... 자식이 아닙니다" 오류로 멈춤 → 이 사이트는 번역하지 않도록 표시
import streamlit.components.v1 as _components  # noqa: E402
_components.html("""<script>
try { const d = window.parent.document;
      d.documentElement.setAttribute('translate', 'no');
      d.documentElement.classList.add('notranslate');
      if (!d.querySelector('meta[name="google"]')) {
        const m = d.createElement('meta'); m.name = 'google'; m.content = 'notranslate'; d.head.appendChild(m); }
} catch (e) {}
</script>""", height=0)

settings = load_settings()

# ---------------- 로그인 (외부 접속 시 필수) ----------------
_pw = settings.get("app_password") or os.environ.get("APP_PASSWORD", "")
if _pw and not st.session_state.get("authed"):
    if settings.get("service_key") and settings.get("kepco_api_key") and not st.session_state.get("kepco_bg"):
        # 한전 공고는 사이트 서버에서만 받을 수 있음 → 로그인 화면만 열려도(자동 깨우기 포함) 받아 둠.
        # 화면에는 아무것도 보여주지 않으므로 비밀번호 없이도 자료가 새지 않음
        st.session_state["kepco_bg"] = True
        try:
            import kepco as _kepco
            _kepco.refresh_if_stale()
        except Exception:  # noqa: BLE001
            pass
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
                               "🏢 경쟁업체", "🔌 한전 입찰", "🔬 추천 검증", "📖 사용법", "⬇️ 데이터 수집", "⚙️ 설정 · API 점검"])

use_sample = st.sidebar.toggle("샘플(연습용) 데이터로 보기", value=not settings["service_key"],
                               help="API 연결 전에 기능을 미리 써보는 가상 데이터입니다.")
db.use_sample(use_sample)
if use_sample:
    if not os.path.exists(SAMPLE_DB_PATH):
        with st.spinner("샘플 데이터 만드는 중..."):
            sample_data.build()
            db.use_sample(True)
    st.sidebar.warning("지금은 **가상 데이터**입니다. 실제 투찰에 쓰지 마세요.")

if not use_sample and db.is_remote():
    try:
        db.query_df("SELECT 1 AS ok")
    except Exception as _e:  # noqa: BLE001  연결 실패 원인을 비밀값 없이 보여줌
        import re as _re
        _msg = _re.sub(r"postgres(?:ql)?(?:\+\w+)?://\S+", "postgresql://***", str(getattr(_e, "orig", _e)))
        _msg = _re.sub(r"password=\S+", "password=***", _msg)
        st.error("데이터베이스(Neon)에 연결하지 못했습니다.")
        st.code(_msg[:600])
        _low = _msg.lower()
        if "quota" in _low or "compute time" in _low:
            st.warning("Neon 무료 사용 시간(월 한도)을 다 쓴 것 같습니다. console.neon.tech → Billing/Usage 화면을 캡처해 주세요.")
        elif "password" in _low or "authentication" in _low:
            st.warning("DB 비밀번호가 맞지 않습니다. Streamlit Secrets 의 DATABASE_URL 이 바뀌지 않았는지 확인하세요.")
        elif "could not translate host" in _low or "name or service" in _low or "invalid" in _low:
            st.warning("DB 주소 형식이 잘못됐습니다. Streamlit Secrets 의 DATABASE_URL 줄을 확인하세요 (따옴표 포함 한 줄).")
        else:
            st.info("잠시 뒤 새로고침해 보세요. 계속되면 이 화면을 캡처해 주세요. 그동안 사이드바의 샘플 데이터로 기능을 볼 수 있습니다.")
        st.stop()

@st.cache_resource(ttl=600, max_entries=1)
def _load(sample: bool):
    # cache_resource: 표를 한 벌만 메모리에 둠 (cache_data 는 저장본+화면마다 복사본으로 2~3벌 → 메모리 부족으로 사이트가 꺼짐)
    db.use_sample(sample)
    return analysis.load_dataset()


@st.cache_resource(ttl=900, max_entries=6)
def _home_calc(_raw, raw_id, keys, lics, _phist):
    """🏠 화면의 공고별 추천 금액·예상 참가 — 같은 자료·같은 공고면 다시 계산하지 않음 (화면 이동 속도)."""
    rows = _raw[_raw["bid_key"].isin(set(keys))]
    lp = analysis.lowest_price(rows)
    picks, exp_n = {}, {}
    for i, r in rows.iterrows():
        if lp.get(i, False):
            continue   # 최저가 내역서 입찰: 사정율 추천 안 함
        kw_ = analysis.bid_keywords(r, list(lics))
        exp_n[r["bid_key"]] = analysis.expected_competition(_raw, r, kw_)
        if pd.notna(r["base_amt_notice"]) and pd.notna(r["lower_rate"]):
            picks[r["bid_key"]] = analysis.pick_bid(_raw, r, kw_, prev=analysis.prev_pick(_phist, r["bid_key"]))
    return picks, exp_n


@st.cache_resource(ttl=900, max_entries=200)
def _competitors(_raw, raw_id, bid_key, sample):
    r = _raw[_raw["bid_key"] == bid_key].iloc[0]
    return analysis.likely_competitors(_ranks(sample), r, _raw)


def _clear_caches():
    st.cache_data.clear()
    _load.clear()
    _home_calc.clear()
    _competitors.clear()


if not use_sample and settings.get("kepco_api_key"):
    try:  # 한전 공고는 사이트 서버에서 받음 (자동 수집 서버에서는 한전 접속이 막힘)
        import kepco as _kepco
        if _kepco.refresh_if_stale():
            _clear_caches()
    except Exception:  # noqa: BLE001  한전 문제로 사이트가 멈추지 않게
        pass

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


raw = _load(use_sample)


@st.cache_data(ttl=3600)
def _kepco_all(sample: bool):
    import kepco
    db.use_sample(sample)
    return kepco.load()


@st.cache_resource(ttl=3600, max_entries=2)
def _ranks(sample: bool):   # 한 벌만 보관 (cache_data 는 부를 때마다 복사본을 만들어 느림)
    db.use_sample(sample)
    return analysis.load_ranks()
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
    try:  # 진행 관리 (관심·참가신청·투찰 완료·메모) — DB 에 저장해 휴대폰·컴퓨터 어디서나 같이 보임
        _trk = db.query_df("SELECT * FROM bid_track")
    except Exception:  # noqa: BLE001
        _trk = pd.DataFrame()
    track = {str(x["bid_no"]): x for x in _trk.to_dict("records")} if len(_trk) else {}

    def _t(no) -> dict:
        return track.get(str(no), {})

    def _memo(no) -> str:
        m = _t(no).get("memo")
        return m.strip() if isinstance(m, str) else ""

    def _tags(r) -> list[str]:
        t, out = _t(r["bid_no"]), []
        if t.get("fav"):
            out.append("⭐관심")
        q = analysis.reg_deadline(pd.DataFrame([r])).iloc[0]
        if pd.notna(q) and q >= now and not t.get("joined"):
            out.append("⛔등록필요")
        if t.get("joined"):
            out.append("📋등록완료")
        if t.get("bid_done") or r["bid_no"] in mine_map:
            out.append("✅투찰완료")
        if _memo(r["bid_no"]):
            out.append("📝메모")
        return out

    if only_ok:
        _all = analysis.apply_filters(raw, keywords=keywords, regions=regions, max_amt=amt_range[1] * 1e8,
                                      min_amt=amt_range[0] * 1e8 or None)
        _all = _all[(_all["open_date"] >= now) & _all["plan_price"].isna()]
        _why = analysis.limit_reasons(_all)
        _out = _all[_why != ""].assign(이유=_why[_why != ""])
        if len(_out):
            with st.expander(f"🚫 참가 조건(실적·PQ·지명·시공능력) 때문에 뺀 공고 {len(_out)}건"):
                st.dataframe(_out[["이유", "name", "org", "open_dt"]].rename(
                    columns={"name": "공고명", "org": "기관", "open_dt": "개찰"}), hide_index=True,
                    use_container_width=True)
                st.caption("잘못 빠진 공고가 있으면 알려주세요. 실적이 있는 분야면 ⚙️ 설정에서 '실적제한 공고도 참가 가능'을 켤 수 있어요.")
    st.markdown(f"투찰 가능 **{len(todo)}건** · 3일 내 마감 "
                f"**{(todo['마감기준'] <= now + pd.Timedelta(days=3)).sum()}건** · "
                f"기초금액 공개 **{todo['base_amt_notice'].notna().sum()}건** · 최근 3일 개찰 **{len(recent)}건**")

    # ── 공고 일정 달력 (참가등록마감·투찰마감·개찰 건수) ──
    wd = "월화수목금토일"
    days = [now.normalize() + pd.Timedelta(days=i) for i in range(-3, 13)]
    qd_all = analysis.reg_deadline(elig).dt.normalize()
    close_all = elig["close_date"].dt.normalize()
    try:  # 한전 공고(참가 가능)도 달력에 함께 — 입찰신청 마감이 중요
        import kepco
        _kb = kepco.load(recent_days=20)
        if not _kb.empty:
            _kb = kepco.filter_bids(_kb, region=_company["region"] or "울산", local_only=True)
            qd_all = pd.concat([qd_all, _kb["attend_close_dt"].dt.normalize()])
            close_all = pd.concat([close_all, _kb["end_dt"].dt.normalize()])
    except Exception:  # noqa: BLE001
        pass
    cal_rows = {"참가등록마감(국방·한전)": qd_all.value_counts(),
                "투찰마감": close_all.value_counts(),
                "개찰": elig["open_date"].dt.normalize().value_counts()}
    head = "".join(
        f"<th style='{'background:#ffe3ea;' if d == now.normalize() else ''}"
        f"{'color:#cf222e;' if d.weekday() == 6 else 'color:#1f6feb;' if d.weekday() == 5 else ''}'>"
        f"{d:%m/%d}<br>{wd[d.weekday()]}</th>" for d in days)
    body = ""
    for name, vc in cal_rows.items():
        cells = ""
        for d in days:
            n = int(vc.get(d, 0))
            bg = "background:#fff5f7;" if d == now.normalize() else ""
            cells += f"<td style='{bg}{'color:#cf222e;font-weight:700' if n else 'color:#bbb'}'>{n}</td>"
        body += f"<tr><th style='text-align:left;white-space:nowrap'>{name}</th>{cells}</tr>"
    st.markdown(
        "<div style='overflow-x:auto'><table style='border-collapse:collapse;font-size:0.8rem;text-align:center;"
        "width:100%'><tr><th></th>" + head + "</tr>" + body + "</table></div>"
        "<style>div[data-testid='stMarkdownContainer'] table td, div[data-testid='stMarkdownContainer'] table th"
        "{border:1px solid #e5e7eb;padding:3px 4px;}</style>", unsafe_allow_html=True)
    future = [d for d in days if d >= now.normalize()]
    close_vc = todo["마감기준"].dt.normalize().value_counts()
    opts = ["전체"] + [f"{d:%m/%d}({wd[d.weekday()]}) {int(close_vc.get(d, 0))}건" for d in future
                     if close_vc.get(d, 0)]
    pick_day = st.pills("투찰마감일로 보기", opts, default="전체", key="cal_day")
    if pick_day and pick_day != "전체":
        md = pick_day.split("(")[0]
        todo = todo[todo["마감기준"].dt.strftime("%m/%d") == md]

    # ── 빠른 필터 (버튼마다 건수) ──
    _reg = analysis.reg_deadline(todo)
    _nos = todo["bid_no"]
    _masks = {
        "오늘 마감": todo["마감기준"].dt.normalize() == now.normalize(),
        "3일 내 마감": todo["마감기준"] <= now + pd.Timedelta(days=3),
        "⛔ 참가등록 필요": (_reg >= now) & ~_nos.map(lambda n: bool(_t(n).get("joined"))),
        "⭐ 관심": _nos.map(lambda n: bool(_t(n).get("fav"))),
        "📝 메모": _nos.map(lambda n: bool(_memo(n))),
        "✅ 투찰 완료": _nos.map(lambda n: bool(_t(n).get("bid_done")) or n in mine_map),
    }
    _chip_lab = {f"{k} {int(m.fillna(False).sum())}": k for k, m in _masks.items()}
    chip = st.pills("빠른 필터", list(_chip_lab), key="quick_chip")
    if chip:
        todo = todo[_masks[_chip_lab[chip]].fillna(False).astype(bool)]

    c_a, c_b = st.columns([2, 1])
    only_base = c_a.toggle("기초금액 공개된 공고만", value=False)
    view = c_b.segmented_control("보기", ["카드", "표"], default="카드", key="today_view",
                                 label_visibility="collapsed") or "카드"
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
    phist = analysis.pick_history()
    picks, exp_n = _home_calc(raw, id(raw), tuple(todo.head(80)["bid_key"]), tuple(sel_lics or _lics), phist)

    def _focus(k):
        e = exp_n.get(k)
        return e is not None and e <= analysis.FOCUS_MAX_PARTICIPANTS
    n_focus = sum(_focus(k) for k in todo.head(80)["bid_key"])
    if n_focus:
        st.success(f"★ 집중 공고 {n_focus}건 — 경쟁이 상대적으로 적은 유형(지역범위·계약방식·금액대)입니다. "
                   "공정 시험에서 이런 공고의 1순위 확률이 공고당 약 2배(4.6% 대 2.1%)였어요. 먼저 챙기세요.")
    order = st.radio("정렬", ["마감 임박순", "★ 경쟁 적은순", "낙찰확률 높은순"], horizontal=True,
                     label_visibility="collapsed")
    if order == "★ 경쟁 적은순":
        todo = todo.assign(_e=todo["bid_key"].map(lambda k: exp_n.get(k) or 1e9))
        todo = todo.sort_values(["_e", "마감기준"], ascending=[True, True])
        st.caption("과거 비슷한 공고(같은 기관 → 울산 지역제한 → 전국 같은 업종·금액대)의 참가업체 수가 적은 순서입니다. "
                   "참가업체가 적을수록 1순위 확률이 높습니다.")
    if order == "낙찰확률 높은순":
        todo = todo.assign(_p=todo["bid_key"].map(lambda k: (picks.get(k) or {}).get("낙찰확률") or -1))
        todo = todo.sort_values(["_p", "마감기준"], ascending=[False, True])
        st.caption("기초금액이 공개되어 투찰금액을 계산할 수 있는 공고부터, 과거 낙찰확률이 높은 순서입니다. "
                   "참가업체가 적은 공고일수록 확률이 높습니다.")

    if len(todo):
        def _dt_s(x):
            x = pd.to_datetime(x, errors="coerce", format="mixed")
            return "" if pd.isna(x) else x.strftime("%m/%d %H:%M")
        rows_t = []
        for _, r in todo.head(80).iterrows():
            pk = picks.get(r["bid_key"])
            rows_t.append({
                "D-day": dday(r["마감기준"]),
                "공고명": r["name"],
                "발주처": r["org"],
                "참가지역": analysis.region_label(r),
                "추정가격": won(r["est_price"]) if pd.notna(r["est_price"]) else "",
                "기초금액": won(r["base_amt"]) if pd.notna(r["base_amt_notice"]) else "미공개",
                "참가등록마감": _dt_s(r.get("qual_dt")) if analysis.is_d2b(r["bid_key"]) else "",
                "투찰마감": _dt_s(r["close_date"]),
                "개찰": _dt_s(r["open_date"]),
                "💰 투찰금액": f"{pk['투찰금액']:,}원" if pk else "",
                "사정율": f"{pk['사정율']:.2f}%" if pk else "",
                "낙찰확률": f"{pk['낙찰확률']}%" if pk and pk.get("낙찰확률") is not None else "",
                "예상 참가": ("★ " if _focus(r["bid_key"]) else "") + (f"{exp_n[r['bid_key']]:.0f}곳"
                                                                    if exp_n.get(r["bid_key"]) else ""),
                "진행": " ".join(_tags(r)),
                "메모": _memo(r["bid_no"]),
                "공고": r["url"] if isinstance(r["url"], str) and r["url"].startswith("http") else None,
            })
        cols_t = ["D-day", "공고명", "진행", "예상 참가", "💰 투찰금액", "사정율", "낙찰확률", "기초금액", "투찰마감", "개찰",
                  "참가등록마감", "발주처", "참가지역", "추정가격", "메모", "공고"]
        _xl = pd.DataFrame(rows_t)[cols_t]
        st.download_button(f"📥 엑셀로 받기 (지금 보이는 {len(_xl)}건)", _xl.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"투찰공고_{now:%Y%m%d}.csv", mime="text/csv", key="xl_today")
    if view == "표" and len(todo):
        st.dataframe(_xl, hide_index=True, use_container_width=True,
                     height=min(38 * (len(rows_t) + 1) + 4, 640),
                     column_config={"공고": st.column_config.LinkColumn("공고", display_text="열기"),
                                    "공고명": st.column_config.TextColumn(width="large")})
        st.caption("투찰금액 근거·경쟁사·투찰 기록은 '카드' 보기에서 확인하세요.")

    for _, r in (todo.head(40) if view == "카드" else todo.head(0)).iterrows():
        with st.container(border=True):
            top = st.columns([5, 1])
            link = f"[{r['name']}]({r['url']})" if isinstance(r["url"], str) and r["url"].startswith("http") else r["name"]
            top[0].markdown(("⭐ " if _focus(r["bid_key"]) else "") + f"**{link}**")
            top[1].markdown(f"**{dday(r['마감기준'])}**")
            _tg = _tags(r)
            if _tg:
                st.markdown(" ".join(f"`{x}`" for x in _tg))
            with st.expander("🗂 진행 관리 (관심 · 참가신청 · 투찰 · 메모)"):
                _tt = _t(r["bid_no"])
                with st.form(f"trk_{r['bid_key']}", border=False):
                    k1, k2, k3 = st.columns(3)
                    f_fav = k1.checkbox("⭐ 관심", value=bool(_tt.get("fav")))
                    f_join = k2.checkbox("참가신청 완료", value=bool(_tt.get("joined")))
                    f_bid = k3.checkbox("투찰 완료", value=bool(_tt.get("bid_done")) or r["bid_no"] in mine_map)
                    f_memo = st.text_area("메모", value=_memo(r["bid_no"]), height=70,
                                          placeholder="현장 확인, 협정 업체, 투찰 근거 등")
                    if st.form_submit_button("저장"):
                        db.upsert_many("bid_track", [{"bid_no": r["bid_no"], "fav": int(f_fav), "joined": int(f_join),
                                                      "bid_done": int(f_bid), "memo": f_memo.strip(),
                                                      "updated_at": db.now()}])
                        st.rerun()
            limit = analysis.region_label(r)
            if exp_n.get(r["bid_key"]):
                st.caption(("★ 집중 — " if _focus(r["bid_key"]) else "") +
                           f"같은 유형 공고 보통 참가 약 {exp_n[r['bid_key']]:.0f}곳 (지역범위·계약방식·금액대 기준)")
            st.caption(f"{r['org']} · 참가지역 {limit} · 투찰마감 "
                       f"{'-' if pd.isna(r['close_date']) else r['close_date'].strftime('%m/%d %H:%M')} · "
                       f"개찰 {r['open_date'].strftime('%m/%d %H:%M')} · {r['contract_method'] or r['method'] or ''}")
            qd = pd.to_datetime(r.get("qual_dt"), errors="coerce", format="mixed") if analysis.is_d2b(r["bid_key"]) else pd.NaT
            if analysis.is_d2b(r["bid_key"]):
                st.caption("🪖 국방전자조달(d2b.go.kr) 공고 — 입찰참가등록을 해야 투찰할 수 있어요")
            if bool(analysis.lowest_price(pd.DataFrame([r])).iloc[0]):
                st.markdown(f":orange[**💲 {analysis.LOWEST_NOTE}**]")
            if bool(analysis.needs_partner(pd.DataFrame([r])).iloc[0]):
                st.markdown(f":violet[**🤝 공동도급 협정 필요 — 면허: {r['licenses']}**  (우리 전기 + 협정 업체)]")
            if pd.notna(qd):
                left = (qd.normalize() - now.normalize()).days
                txt = f"참가등록 마감 {qd.strftime('%m/%d %H:%M')}"
                if qd < now:
                    st.caption(f"⛔ {txt} (지남 — 등록했으면 투찰 가능)")
                elif left <= 1:
                    st.markdown(f":red[**⛔ {txt} ({'오늘' if left == 0 else '내일'})** — 아직 안 했다면 국방전자조달에서 참가등록하세요]")
                else:
                    st.caption(f"⛔ {txt} (D-{left})")
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
            if bool(r.get("lower_rate_diff")):
                st.warning(f"⚠️ 국방 하한율 88.745%로 계산했지만, 공고 상세에는 {r['lower_rate_api']:.3f}%로 적혀 있어요. "
                           "공고문을 확인하세요.")
            pc_min = analysis.pure_cost_min(r)
            st.caption(a_value_text(r) + (f" · 순공사원가 98% 하한 {pc_min:,}원" if pc_min else ""))
            if bool(analysis.lowest_price(pd.DataFrame([r])).iloc[0]):
                st.caption("💲 최저가 내역서 입찰이라 사정율 추천 금액은 표시하지 않습니다. 위 첨부(내역서·도면)를 검토해 견적을 내세요.")
                continue
            if pd.isna(r["lower_rate"]):
                st.warning("하한율 정보가 없어 투찰금액을 계산할 수 없습니다. 공고문을 확인하세요.")
                continue
            pk = picks.get(r["bid_key"]) or analysis.pick_bid(raw, r, analysis.bid_keywords(r, sel_lics or _lics),
                                                              prev=analysis.prev_pick(phist, r["bid_key"]))
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
            hl = analysis.history_line(phist, r["bid_key"])
            if hl:
                st.caption(f"🔁 추천 변화: {hl} (데이터가 늘어 확실히 더 나은 지점이 생겼을 때만 바뀜)")
            qok, qmsg = pk.get("적격심사") or (None, "")
            if qok is False:
                st.warning("⚠️ " + qmsg)
            st.markdown("**📌 근거**  \n" + "  \n".join("• " + x for x in analysis.evidence_lines(pk)))
            ex = pk["근거상세"]["examples"]
            if ex:
                st.markdown("최근 비슷한 공고에 이 사정율로 넣었다면  \n" + "  \n".join(
                    f"– {x['공고명'][:28]} ({x['개찰일']}): 실제 {x['실제']:.2f} / 낙찰자 {x['1순위']:.2f} → **{x['결과']}**"
                    for x in ex))
            with st.expander("🏢 예상 경쟁사"):
                lab, comp = _competitors(raw, id(raw), r["bid_key"], use_sample)
                if comp.empty:
                    st.caption("개찰순위 데이터가 쌓이면 표시됩니다.")
                else:
                    st.caption(f"기준: {lab}의 개찰 상위권에 자주 나온 업체")
                    st.dataframe(comp, hide_index=True, use_container_width=True,
                                 column_config={"평균 사정율": st.column_config.NumberColumn(format="%.3f"),
                                                "편차": st.column_config.NumberColumn(format="%.3f")})
                    near = comp[(comp["평균 사정율"] - pk["사정율"]).abs() <= 0.1]
                    if len(near):
                        st.caption("⚠️ 추천 사정율 근처(±0.1%p)를 자주 노리는 업체: " + ", ".join(near["업체명"].astype(str)))
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
                                                "checked_at": None, "created_at": db.now(),
                                                "rec_amount": pk["투찰금액"], "amount_source": "추천 금액으로 기록"}])
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
                                        "created_at": db.now(), "amount_source": "투찰 계산기에서 기록"}])
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
                                        "my_rank": None, "checked_at": None, "created_at": db.now(),
                                        "amount_source": "직접 입력"}])
            _clear_caches()
            st.success("저장했습니다.")

    # 개찰 시간이 지났는데 결과가 없는 우리 투찰은 들어올 때 바로 조회 (10분에 한 번)
    if not use_sample and settings.get("service_key"):
        _last = st.session_state.get("_my_refresh_at")
        _force = st.button("🔄 개찰결과 지금 불러오기", help="개찰 시간이 지난 우리 투찰의 순위·실제 사정율을 바로 조회합니다.")
        if _force or _last is None or (pd.Timestamp.now() - _last).total_seconds() > 1800:
            st.session_state["_my_refresh_at"] = pd.Timestamp.now()
            with st.spinner("개찰결과 확인 중…"):
                try:
                    # 자동 확인은 가볍게(최대 5건 + 새 공고 10건), 버튼을 누르면 넉넉하게
                    _msg = collector.refresh_my_results(df=raw, max_bids=10 if _force else 5,
                                                        scan_limit=30 if _force else 10)
                except Exception as e:  # noqa: BLE001
                    _msg = f"개찰결과 확인 실패: {e}"
            if not ("개찰 확인 0건" in _msg and "새로 찾은 우리 투찰 0건" in _msg):
                _clear_caches()
                raw = _load(use_sample)
            st.caption(f"개찰결과 자동 확인: {_msg}")
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
    for c_ in ("rec_amount", "amount_source"):
        if c_ not in v:
            v[c_] = None
    vv = v.sort_values("open_date", ascending=False).copy()
    first_amt = pd.to_numeric(vv["win_ref_amt"] if "win_ref_amt" in vv else vv["first_amt"], errors="coerce")
    vv["1순위 금액"] = first_amt
    vv["실제-1순위"] = pd.to_numeric(vv["amount"], errors="coerce") - first_amt
    vv["분석-1순위"] = pd.to_numeric(vv["rec_amount"], errors="coerce") - first_amt
    a_ = vv["a_value"].fillna(0)
    r_ = vv["lower_rate"] / 100
    vv["분석 사정율"] = ((pd.to_numeric(vv["rec_amount"], errors="coerce") - a_) / r_ + a_) / vv["base_amt"] * 100
    show = vv[["결과", "name", "open_dt", "amount", "rec_amount", "1순위 금액", "실제-1순위", "분석-1순위",
               "my_rank", "participants", "sajung", "분석 사정율", "sajung_bid", "winner_sajung", "first_company",
               "amount_source", "org", "memo", "bid_key"]].rename(columns={
            "name": "공고명", "org": "기관", "open_dt": "개찰일시", "amount": "실제 투찰금액",
            "amount_source": "금액 출처", "rec_amount": "분석 금액", "sajung": "우리 사정율",
            "sajung_bid": "실제 사정율", "winner_sajung": "1순위 사정율", "my_rank": "우리 순위",
            "participants": "참가수", "first_company": "1순위 업체", "memo": "메모", "bid_key": "공고번호"})
    money = st.column_config.NumberColumn(format="localized")
    st.dataframe(show, hide_index=True, use_container_width=True,
                 column_config={"실제 투찰금액": money, "분석 금액": money, "1순위 금액": money,
                                "실제-1순위": st.column_config.NumberColumn(format="localized", help="양수(+)면 1순위보다 비싸게 넣음"),
                                "분석-1순위": st.column_config.NumberColumn(format="localized",
                                                                         help="분석 금액대로 넣었다면 1순위와 차이"),
                                "우리 사정율": st.column_config.NumberColumn(format="%.3f"),
                                "분석 사정율": st.column_config.NumberColumn(format="%.3f"),
                                "실제 사정율": st.column_config.NumberColumn(format="%.3f"),
                                "1순위 사정율": st.column_config.NumberColumn(format="%.3f")})
    st.caption("실제 투찰금액 = 개찰순위에서 우리 사업자번호로 찾은 금액 · 분석 금액 = 개찰 전 시스템 추천 금액 · "
               "1순위 금액 = 최종 낙찰자(없으면 개찰 1순위) 금액. 1순위가 되려면 '실제 사정율 이상 + 1순위 금액보다 낮게'.")
    st.caption("우리 금액: 개찰 후에는 개찰순위에서 사업자번호로 찾은 **실제 투찰금액**으로 자동으로 바뀝니다 "
               "(금액 출처 '개찰순위(실제)'). 개찰 전이거나 못 찾았으면 기록한 금액입니다.")
    with st.expander("✏️ 실제 투찰금액 고치기"):
        opts_e = [f"{r.bid_key} | {r.name}" for r in v.sort_values("open_date", ascending=False).itertuples()]
        pe = st.selectbox("공고", opts_e, key="edit_bid")
        cur = v.set_index("bid_key").loc[pe.split(" | ")[0]]
        new_amt = st.number_input("실제 투찰금액(원)", min_value=0.0, step=1.0, format="%.0f",
                                  value=float(cur["amount"]) if pd.notna(cur["amount"]) else 0.0, key="edit_amt")
        if st.button("금액 저장", key="edit_save") and new_amt > 0:
            db.execute("UPDATE my_bids SET amount = :a, sajung = NULL, amount_source = '직접 수정', "
                       "rec_amount = COALESCE(rec_amount, amount) WHERE bid_key = :k",
                       {"a": new_amt, "k": pe.split(" | ")[0]})
            _clear_caches()
            st.success("저장했습니다.")
            st.rerun()
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
elif page == "🔌 한전 입찰":
    import kepco
    st.header("🔌 한전 입찰 (전력그룹사 전자입찰)")
    st.caption("한전 전력데이터개방포털 API로 받은 공고입니다. 개찰 순위·1순위 금액은 API에 없어서, "
               "개찰 후 공개되는 '사정금액'으로 과거 사정율 분포를 보여줍니다. 투찰은 한전 SRM(srm.kepco.net)에서 합니다.")
    if not settings.get("kepco_api_key"):
        st.warning("한전 API 인증키가 없습니다. 사용법: 전력데이터개방포털(bigdata.kepco.co.kr) 회원가입 → "
                   "API 인증키 신청 → 받은 40자리 키를 Streamlit Secrets 와 GitHub Secrets 에 "
                   "`KEPCO_API_KEY` 이름으로 넣기.")
    kb_all = _kepco_all(use_sample)
    if kb_all.empty:
        st.info("아직 받은 한전 공고가 없습니다. 인증키를 넣은 뒤 아래 버튼으로 받거나, 다음 자동 수집을 기다리세요.")
    k1, k2 = st.columns(2)
    if k1.button("한전 공고 지금 받기 (최근 60일)", disabled=not settings.get("kepco_api_key")):
        with st.spinner("받는 중…"):
            try:
                n = kepco.collect(date.today() - timedelta(days=60), date.today())
                st.success(f"{n:,}건 받았습니다.")
                _clear_caches()
                kb_all = _kepco_all(use_sample)
            except Exception as e:  # noqa: BLE001
                st.error(f"실패: {e}")
    k_mode = st.radio("화면", ["📋 공고·일정", "🧮 투찰금액 계산", "📥 개찰결과·투찰 이력 넣기"], horizontal=True,
                      key="kepco_mode", label_visibility="collapsed")
    if not kb_all.empty and k_mode.startswith("📋"):
        reg = db.company()["region"] or "울산"
        scope_k = k2.radio("지역", [f"참가 가능 ({reg} + 전국)", f"{reg}만", "전체"], horizontal=True,
                           label_visibility="collapsed")
        local_only = not scope_k.startswith("전체")
        kw_extra = st.text_input("공고명 키워드 (쉼표 구분, 비우면 전기·배전·송전 등 기본)", "")
        kws = [x.strip() for x in kw_extra.split(",") if x.strip()] or None
        kind_k = st.radio("구분", ["전체", "공사", "용역"], horizontal=True, key="kepco_kind",
                          help="용역은 ⚙️ 설정의 '한전 용역 키워드'(기본: 광학카메라·간이 PD·콘크리트전주 내부진단)에 맞는 것만")
        kb = kepco.filter_bids(kb_all, region=reg, keywords=kws, local_only=local_only, kind=kepco.KINDS[kind_k],
                               include_national=scope_k.startswith("참가"))
        now = pd.Timestamp.now()
        dl = kb["end_dt"].fillna(kb["attend_close_dt"])
        up = kb[dl >= now].assign(_dl=dl).sort_values("_dl")
        n_sj = int(kb["sajung"].notna().sum())
        st.markdown(f"진행 중 **{len(up)}건** · 받은 공사 공고 **{len(kb)}건** · 사정율 확인 **{n_sj}건**")
        if n_sj == 0:
            st.info("한전 API는 개찰 결과(예정가격)를 주지 않아서 아직 사정율 분석은 할 수 없습니다. "
                    "지금은 공고·마감 일정 확인과 텔레그램 알림용으로 쓰세요. 투찰금액은 공고문의 하한율·A값으로 "
                    "🧮 투찰 계산기에서 계산할 수 있습니다.")
        rows_k = []
        for _, r in up.head(100).iterrows():
            stt = kepco.sajung_stats(kb_all, r, reg) if pd.notna(r["base_amt"]) else None
            amt = None
            if stt and pd.notna(r["base_amt"]):
                amt = r["base_amt"] * stt["중앙"] / 100
            files = []
            try:
                files = json.loads(r["files"]) if isinstance(r["files"], str) else []
            except ValueError:
                pass
            rows_k.append({
                "D-day": f"D-{max((r['_dl'].normalize() - now.normalize()).days, 0)}",
                "공고명": r["name"], "지역": r.get("지역", ""), "발주": f"{r['발주사']} {r['place'] or ''}".strip(),
                "추정가격": won(r["presumed_price"]) if pd.notna(r["presumed_price"]) else "",
                "기초금액": won(r["base_amt"]) if pd.notna(r["base_amt"]) else "미공개",
                "입찰신청마감": "" if pd.isna(r["attend_close_dt"]) else r["attend_close_dt"].strftime("%m/%d %H:%M"),
                "입찰마감": "" if pd.isna(r["end_dt"]) else r["end_dt"].strftime("%m/%d %H:%M"),
                "과거 사정율(중앙)": f"{stt['중앙']:.2f}% ({stt['기준']} {stt['n']}건)" if stt else "",
                "예정가격 예상(중앙)": won(amt) if amt else "",
                "낙찰방법": r["낙찰방법"] or "", "상태": r["상태"] or "",
                "공고문": files[0][1] if files else None,
            })
        if rows_k:
            tbl = pd.DataFrame(rows_k)
            if n_sj == 0:
                tbl = tbl.drop(columns=["과거 사정율(중앙)", "예정가격 예상(중앙)"])
            st.dataframe(tbl, hide_index=True, use_container_width=True,
                         column_config={"공고문": st.column_config.LinkColumn("공고문", display_text="열기"),
                                        "공고명": st.column_config.TextColumn(width="large")})
            if n_sj:
                st.caption("예정가격 예상 = 기초금액 × 과거 사정율 중앙값. 투찰금액은 (예정가격 − A값) × 하한율 + A값 이므로 "
                           "공고문의 하한율·A값을 🧮 투찰 계산기에 넣어 계산하세요.")
        else:
            st.info("진행 중인 공고가 없습니다.")
        done_k = kb[kb["sajung"].notna()]
        if len(done_k) >= 5:
            st.subheader("📊 한전 공사 사정율 분포")
            fig = px.histogram(done_k, x="sajung", nbins=40, labels={"sajung": "사정율(%)"})
            fig.update_layout(height=320, margin=dict(t=20))
            st.plotly_chart(fig, use_container_width=True)
            by_place = (done_k.groupby("place")["sajung"].agg(["count", "median", "mean"])
                        .sort_values("count", ascending=False).head(20).reset_index()
                        .rename(columns={"place": "사업소", "count": "건수", "median": "중앙", "mean": "평균"}))
            st.dataframe(by_place, hide_index=True, use_container_width=True,
                         column_config={"중앙": st.column_config.NumberColumn(format="%.3f"),
                                        "평균": st.column_config.NumberColumn(format="%.3f")})
    if not kb_all.empty:
        reg_k = db.company()["region"] or "울산"
        cons = kepco.filter_bids(kb_all, region=reg_k, local_only=False)
        nowk = pd.Timestamp.now()

        def _lab(r):
            d = "" if pd.isna(r["end_dt"]) else r["end_dt"].strftime("%m/%d")
            k = f"[{r['구분']}] " if "구분" in r and pd.notna(r.get("구분")) else ""
            return f"{d} {k}{r['name'][:40]} ({r['bid_no']})"

        if k_mode.startswith("🧮"):
            st.subheader("🧮 한전 투찰금액 계산")
            upk_all = cons[cons["end_dt"] >= nowk].sort_values("end_dt")
            n_c = int((upk_all["구분"] == "공사").sum()) if "구분" in upk_all else 0
            n_s = int((upk_all["구분"] == "용역").sum()) if "구분" in upk_all else 0
            _cnt = {"공사": n_c, "용역": n_s, "전체": n_c + n_s}
            kind_c = st.radio("구분", ["공사", "용역", "전체"], horizontal=True, key="kc_kind",
                              format_func=lambda k: f"{k} ({_cnt[k]}건)", help="용역은 ⚙️ 설정의 '한전 용역 키워드'(기본: 광학카메라·간이 PD·콘크리트전주 "
                                                   "내부진단)에 맞는, 마감 전 공고만 나옵니다.")
            upk = upk_all if kind_c == "전체" or "구분" not in upk_all else upk_all[upk_all["구분"] == kind_c]
            if upk.empty:
                st.caption(f"지금 마감 전인 한전 {kind_c} 공고가 없습니다. "
                           "(용역 공고는 자주 나오지 않아요. 나오면 여기와 텔레그램 알림에 뜹니다)")
            else:
                opt = st.selectbox("공고", upk.index, format_func=lambda i: _lab(upk.loc[i]), key="kc_bid")
                r = upk.loc[opt]
                rec = kepco.recommend(kb_all, r, reg_k)
                c1, c2, c3, c4 = st.columns(4)
                base_k = c1.number_input("예비가격 기초금액(원)", 0.0, 1e12,
                                         float(r["base_amt"]) if pd.notna(r["base_amt"]) else 0.0, 1000.0, format="%.0f")
                lr_k = c2.number_input("하한율(%)", 0.0, 100.0, 87.745, 0.001, format="%.3f",
                                       help="공고문(입찰공고서)의 낙찰하한율")
                a_k = c3.number_input("A값(원)", 0.0, 1e12, 0.0, 1000.0, format="%.0f", help="공고문에 있으면 입력")
                s_k = c4.number_input("사정율(%)", 95.0, 105.0, float(rec["사정율"]) if rec else 100.0, 0.01, format="%.2f")
                if base_k > 0:
                    st.metric("💰 투찰금액", f"{analysis.bid_amount(base_k, s_k, lr_k, a_k):,}원")
                if rec:
                    st.caption(f"참고 사정율 {rec['사정율']:.2f}% — {rec.get('기준', '')} 개찰 결과 {rec['n']}건 기준 과거 낙찰확률 "
                               f"{rec['낙찰확률']:.1f}% (같은 사업소 결과가 30건 이상이면 그 사업소만 비교)")
                    st.caption(f"이 공고는 **{r.get('구분', '공사')}** → 같은 종류(한전 {r.get('구분', '공사')}) 결과만 비교에 씁니다.")
                    st.warning("⚠️ 한전 용역은 우리 투찰 이력 260건으로 공정하게 시험했을 때, 이 방식(과거에 가장 많이 이긴 사정율)이 "
                               "무작위보다 낫지 않았습니다. 참고용으로만 보세요. (참가 250곳 안팎·낙찰 구간 폭 0.02%p 수준이라 "
                               "사정율 맞히기보다 경쟁사 투찰 분포를 피하는 방식이 필요 — 개찰결과(업체별 금액)가 쌓이면 다음 단계로 진행)")
                else:
                    st.caption("한전 개찰 결과가 10건 이상 기록되면 추천 사정율이 나옵니다. 그 전에는 사정율을 직접 정하세요.")

        if k_mode.startswith("📥"):
            st.subheader("📋 SRM 개찰결과 화면 붙여넣기 (가장 쉬운 방법)")
            st.caption("SRM → 입찰/계약 → 정보공개 → 개찰정보공개 → 공고의 '개찰결과보기'를 연 다음, "
                       "그 화면을 클릭하고 Ctrl+A(전체 선택) → Ctrl+C(복사) → 아래 칸에 Ctrl+V. "
                       "공고번호·예정가격·적격률·업체별 투찰금액을 알아서 읽어 저장합니다. 한 건씩 반복하세요.")
            st.caption("여러 공고를 한 칸에 계속 이어서 붙여넣어도 됩니다 (공고번호마다 나눠서 읽음). 다 붙인 뒤 한 번만 저장하세요.")
            pasted_r = st.text_area("개찰결과 화면 붙여넣기 (여러 건 가능)", height=180, key="srm_result_paste")
            if pasted_r.strip():
                many = kepco.parse_many(pasted_r)
                if not many:
                    st.warning("공고번호·예정가격을 찾지 못했습니다. 개찰결과 화면 전체(Ctrl+A)를 복사했는지 확인하세요.")
                else:
                    st.dataframe(pd.DataFrame([{"공고번호": x["bid_no"], "예정가격": f"{x['plan_price']:,.0f}",
                                                "적격률": x["lower_rate"], "순위 목록": len(x["bidders"]),
                                                "적격률 탈락": len(x["below"]),
                                                "1순위 금액": f"{x['bidders'][0]['amount']:,.0f}" if x["bidders"] else ""}
                                               for x in many]), hide_index=True, use_container_width=True)
                    if st.button(f"{len(many)}건 저장", type="primary", key="srm_result_save"):
                        msgs = []
                        for x in many:
                            try:
                                msgs.append(kepco.save_parsed_result(x, db.company()["bizno"]))
                            except ValueError as e:
                                msgs.append(f"{x.get('bid_no')}: {e}")
                        _clear_caches()
                        st.success(f"{len(many)}건 저장했습니다.")
                        for m_ in msgs:
                            st.caption("• " + m_)

            st.subheader("📥 투찰 이력 엑셀 올리기 (모두부드·SRM)")
            st.caption("한전 SRM의 우리 회사 입찰 결과(개찰 결과)를 엑셀로 올리거나 화면 표를 복사해 붙여넣으면, "
                       "공고마다 예정가격·1순위 금액을 한 번에 저장해서 한전 사정율·낙찰확률 분석에 씁니다.")
            how_up = st.radio("방법", ["엑셀·CSV 파일 올리기", "화면 표 복사해서 붙여넣기"], horizontal=True, key="srm_how")
            up, pasted = None, ""
            if how_up.startswith("화면"):
                st.caption("SRM 결과 화면의 표를 제목 줄부터 마우스로 끌어 선택 → Ctrl+C → 아래 칸에 Ctrl+V. "
                           "여러 화면이면 나눠서 여러 번 해도 됩니다.")
                pasted = st.text_area("여기에 붙여넣기", height=160, key="srm_paste")
            else:
                up = st.file_uploader("엑셀(xlsx·xls) 또는 CSV 파일 — 모두부드 '투찰 이력' 엑셀도 그대로 올리면 됩니다",
                                      type=["xlsx", "xls", "csv"], key="srm_up", accept_multiple_files=False)
            if up is not None or pasted.strip():
                try:
                    import io as _io
                    if pasted.strip():
                        txt = pasted.strip()
                        sep = "\t" if "\t" in txt else ("," if txt.count(",") > txt.count("  ") else r"\s{2,}")
                        raw_up = pd.read_csv(_io.StringIO(txt), sep=sep, engine="python", dtype=str)
                    else:
                        raw_up = kepco.read_table_file(up, up.name)
                    # 제목 줄이 위에 몇 줄 있는 양식 대비: '공고번호'가 들어 있는 줄을 칸 이름으로
                    if not any("공고" in str(c) or "번호" in str(c) for c in raw_up.columns):
                        for i in range(min(10, len(raw_up))):
                            if raw_up.iloc[i].astype(str).str.contains("공고|입찰번호").any():
                                raw_up.columns = raw_up.iloc[i].astype(str)
                                raw_up = raw_up.iloc[i + 1:].reset_index(drop=True)
                                break
                    st.caption(f"{len(raw_up):,}줄 · 앞부분 미리보기")
                    st.dataframe(raw_up.head(5), use_container_width=True)
                    guess = kepco.guess_columns(list(raw_up.columns))
                    cols_opt = ["(없음)"] + list(raw_up.columns)
                    mapping = {}
                    mcols = st.columns(2)
                    for i_, (k_, (label_, _)) in enumerate(kepco.IMPORT_FIELDS.items()):
                        dflt = cols_opt.index(guess[k_]) if k_ in guess and guess[k_] in cols_opt else 0
                        sel = mcols[i_ % 2].selectbox(label_, cols_opt, index=dflt, key=f"map_{k_}")
                        if sel != "(없음)":
                            mapping[k_] = sel
                    lr_default = st.number_input("하한율 칸이 없을 때 쓸 하한율(%)", 0.0, 100.0, 87.745, 0.001, format="%.3f")
                    if "bid_no" not in mapping or "plan_price" not in mapping:
                        st.warning("최소한 '공고번호'와 '예정가격' 칸은 골라야 합니다.")
                    elif st.button("불러오기", type="primary", key="srm_import"):
                        n_ok, n_skip = kepco.import_results(raw_up, mapping, lr_default)
                        _clear_caches()
                        matched = len(set(raw_up[mapping["bid_no"]].astype(str).str.strip())
                                      & set(kb_all["bid_no"].astype(str)))
                        st.success(f"{n_ok:,}건 저장 (예정가격 없는 줄 {n_skip:,}건 건너뜀) · "
                                   f"한전 공고 목록과 공고번호가 맞은 것 {matched:,}건")
                        if n_ok and matched == 0:
                            st.info("공고번호 형식이 한전 API와 다를 수 있어요. 이 화면을 캡처해 보내주시면 맞춰 드릴게요. "
                                    "기초금액 칸이 있으면 공고번호가 안 맞아도 사정율은 계산됩니다.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"파일을 읽지 못했습니다: {e}")

            st.subheader("📝 한전 개찰 결과 기록")
            st.caption("SRM에서 개찰 결과(예정가격·1순위 금액)를 보고 적어 두면, 한전 사정율·낙찰확률 분석에 쓰입니다. "
                       "우리가 참가하지 않은 공고도 결과를 볼 수 있으면 적어 주세요. 많을수록 정확해집니다.")
            pastk = cons[(cons["end_dt"] < nowk) & (cons["end_dt"] >= nowk - pd.Timedelta(days=120))] \
                .sort_values("end_dt", ascending=False)
            if pastk.empty:
                st.caption("최근 120일 안에 마감된 한전 공사 공고가 없습니다.")
            else:
                with st.form("kepco_result"):
                    opt2 = st.selectbox("공고", pastk.index, format_func=lambda i: _lab(pastk.loc[i]))
                    f1, f2, f3 = st.columns(3)
                    plan_k = f1.number_input("예정가격(원)", 0.0, 1e12, 0.0, 1000.0, format="%.0f")
                    first_k = f2.number_input("1순위(낙찰) 금액(원)", 0.0, 1e12, 0.0, 1000.0, format="%.0f")
                    part_k = f3.number_input("참가업체 수", 0, 10000, 0)
                    g1, g2, g3, g4 = st.columns(4)
                    lr2 = g1.number_input("하한율(%)", 0.0, 100.0, 87.745, 0.001, format="%.3f")
                    a2 = g2.number_input("A값(원)", 0.0, 1e12, 0.0, 1000.0, format="%.0f")
                    our_k = g3.number_input("우리 투찰금액(원, 선택)", 0.0, 1e12, 0.0, 1000.0, format="%.0f")
                    rank_k = g4.number_input("우리 순위(선택)", 0, 10000, 0)
                    if st.form_submit_button("저장", type="primary"):
                        if plan_k <= 0:
                            st.error("예정가격을 입력하세요.")
                        else:
                            db.upsert_many("kepco_results", [{
                                "bid_no": pastk.loc[opt2, "bid_no"], "plan_price": plan_k,
                                "first_amt": first_k or None, "lower_rate": lr2, "a_value": a2,
                                "participants": part_k or None, "our_amount": our_k or None, "our_rank": rank_k or None,
                                "memo": "", "created_at": db.now()}])
                            b0 = pastk.loc[opt2, "base_amt"]
                            msg = f" (사정율 {plan_k / b0 * 100:.3f}%)" if pd.notna(b0) and b0 else ""
                            st.success("저장했습니다" + msg)
            nrec = int(db.query_df("SELECT COUNT(*) AS n FROM kepco_results")["n"].iloc[0])
            st.caption(f"지금까지 기록한 한전 개찰 결과: {nrec}건")

    with st.expander("🔍 한전 API 원본 확인 (항목 점검용)"):
        if st.button("최근 7일 원본 1건 보기", disabled=not settings.get("kepco_api_key")):
            try:
                raw_k = kepco.fetch(date.today() - timedelta(days=7), date.today())
                st.caption(f"{len(raw_k)}건 응답")
                if raw_k:
                    st.json(raw_k[0])
            except Exception as e:  # noqa: BLE001
                st.error(str(e))

elif page == "🔬 추천 검증":
    st.header("🔬 추천 방식 검증 (백테스트)")
    st.markdown(
        "지난 공고 하나하나에 대해 **그 공고 이전 데이터만으로** 사정율을 골랐다면 실제로 어땠는지 시험합니다. "
        "미래 데이터를 쓰지 않으므로, 실제로 이 방식대로 넣었을 때의 성적과 같습니다.")
    need_data()
    st.subheader("📈 실제 추천 성적표")
    st.caption("매일 개찰 전에 기록한 추천 금액대로 넣었다면 어땠는지. 개찰 후에 다시 계산한 값이 아니라 "
               "그때 보여드린 금액 그대로 채점합니다.")
    sc = analysis.pick_scorecard(raw)
    sc = sc[sc["결과"] != "결과 대기"] if not sc.empty else sc
    if sc.empty:
        st.info("아직 채점할 공고가 없습니다. 추천을 기록한 공고가 개찰되면 여기에 쌓입니다.")
    else:
        st.markdown("**" + analysis.scorecard_summary(sc) + "**")
        show = sc.assign(개찰=sc["open_date"].dt.strftime("%m/%d"),
                         순위=sc["가상 순위"].map(lambda x: "" if pd.isna(x) else ("상위권 밖" if x == float("inf") else f"{int(x)}위")))
        st.dataframe(show[["개찰", "name", "org", "sajung_pick", "amount", "sajung", "winner_sajung", "결과", "순위"]]
                     .rename(columns={"name": "공고명", "org": "발주처", "sajung_pick": "추천 사정율", "amount": "추천 금액",
                                      "sajung": "실제 사정율", "winner_sajung": "낙찰자 사정율"}),
                     hide_index=True, use_container_width=True,
                     column_config={"추천 금액": st.column_config.NumberColumn(format="%,.0f"),
                                    "추천 사정율": st.column_config.NumberColumn(format="%.2f"),
                                    "실제 사정율": st.column_config.NumberColumn(format="%.3f"),
                                    "낙찰자 사정율": st.column_config.NumberColumn(format="%.3f")})
    st.markdown("---")
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
            _clear_caches()
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

# ---------------- 사용법 ----------------
elif page == "📖 사용법":
    st.header("📖 사용법")
    st.markdown("""
### ☀️ 매일 할 일 (5분)
1. **아침 텔레그램 확인** — 새 공고, 기초금액 공개(투찰금액 포함), 오늘 마감, 개찰 결과가 한 번에 옵니다.
2. **🏠 오늘의 투찰** 열기 — 마감 임박순(또는 낙찰확률 높은순)으로 넣을 공고와 투찰금액이 나옵니다.
3. **⛔ 참가등록 마감**(국방·한전 공고)이 빨간색이면 국방전자조달·한전 SRM에서 입찰참가등록부터 합니다. 등록하지 않으면 투찰할 수 없어요.
4. **공고문 확인** — 📎 공고문 링크를 열어 **하한율·A값**이 화면과 같은지 대조합니다.
5. 나라장터에서 **💰 투찰금액**대로 투찰 → 사이트에서 **📝 이 금액으로 투찰 기록**을 누릅니다.
   (기록해 두면 개찰 후 결과가 자동으로 채워지고, 마감 리마인더도 더 오지 않습니다)

> 낙찰은 추첨 요소가 커서 한 공고의 낙찰확률은 1% 안팎이 정상입니다.
> **낙찰 횟수 ≈ 넣은 공고들의 낙찰확률 합계** — 참가 가능한 공고에 꾸준히, 추천 금액으로 넣는 것이 가장 확실합니다.

### 🔢 숫자 읽는 법
| 표시 | 뜻 |
|---|---|
| 💰 투찰금액 | 과거 비슷한 공고에서 낙찰 가능성이 가장 높았던 사정율로 계산한 금액 |
| 사정율 | 예정가격 ÷ 기초금액. 투찰금액 = (기초금액×사정율 − A값) × 하한율 + A값 |
| 신뢰도 🟢🟡🔴 | 비교할 과거 데이터의 양과 확실성. 🔴이면 참고만 |
| 낙찰확률 | 과거 공고에 이 사정율로 넣었다면 낙찰됐을 비율 |
| 무작위의 ○배 | 아무렇게나 넣었을 때보다 몇 배 유리한지 (2배 이상이면 의미 있음) |
| 근거 | 같은 기관 → 울산 지역제한 공고 → 전국 같은 업종 순으로 과거 몇 건 중 몇 건 낙찰/하한 미달이었는지 |
| 하한 미달이 많음 | 정상입니다. 낙찰자가 되려면 하한선 바로 위를 노려야 해서 자주 하한 미달이 납니다 |
| 🏢 예상 경쟁사 | 이 기관·지역 공고의 상위권에 자주 나온 업체와 그 업체가 보통 노리는 사정율 |

### 🔔 알림 종류 (텔레그램)
- 🆕 새 공고 · 💰 기초금액 공개(투찰금액·근거) · ⏰ 오늘 투찰 마감 · ⛔ 국방·한전 참가등록 마감 임박(전날·당일)
- 📊 개찰 결과 · 📝 우리 투찰 결과 · 📈 주간 성적(월요일) · 🔬 추천 방식 검증(월요일)
- 🚨 자동 수집 오류 · 저장 공간 경고

### ⏱ 자동으로 돌아가는 일
| 언제 | 무엇을 |
|---|---|
| 매일 새벽 5시경 | 전체 수집 + 과거 1년치 채우기 + 경쟁업체 순위 + 우리 결과 확인 + 저장 공간 정리 + 알림 |
| 평일 9·13·17시경 | 빠른 수집(오늘 공고·기초금액·개찰결과) + 알림 |
| 매주 월요일 새벽 | 추천 방식 자동 검증(더 좋은 방식이 확실하면 교체) + 주간 성적 리포트 |
| 코드가 바뀔 때마다 | 계산·화면 자동 점검 |

### 🛠 처음 한 번 설정 (⚙️ 설정 → 🏢 우리 회사)
- **사업자번호** — 개찰순위에서 우리 순위·낙찰 여부를 자동으로 찾습니다.
- **소재지·보유 면허** — 참가 가능한 공고를 고르는 기준입니다. 면허를 추가하면 해당 업종 공고도 들어옵니다.
- **적격심사 통과 가능 금액** — 한도 계산 도우미에 실적·배수·경영상태를 넣으면 계산됩니다.

### 🆘 문제가 생기면
- 화면에 빨간 오류 → 새로고침. 계속되면 share.streamlit.io → 앱 ⋮ → **Reboot app**
- 🚨 수집 오류 알림 → 알림 속 링크 화면을 캡처해서 Claude 에게 보내기
- 숫자가 이상함 → **⬇️ 데이터 수집**의 🩺 데이터 점검표를 캡처해서 보내기
""")

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
        if st.button("🧹 저장 공간 지금 정리하기", type="primary" if mb > lim * 0.8 else "secondary"):
            with st.spinner("정리 중… (1~3분 걸릴 수 있어요)"):
                kw = analysis.company_keywords()
                n1 = db.prune_prelim(kw)
                n2 = db.prune_nontarget(kw) + db.prune_orphans()
                try:
                    db.vacuum()
                except Exception as e:  # noqa: BLE001
                    st.caption(f"VACUUM 생략: {e}")
                _clear_caches()
            after = db.size_mb() or 0
            st.success(f"정리 완료: 예비가격 상세 {n1:,}행 · 다른 업종 공고 자료 {n2:,}행 삭제 → 현재 {after:,.0f}MB")
            st.caption("지운 자리는 새 자료가 다시 쓰므로 더 늘지는 않습니다. 숫자(MB)가 실제로 줄어드는 압축은 "
                       "사이트가 멈추지 않도록 매일 새벽 자동 수집 때 합니다.")
        with st.expander("테이블별 용량"):
            st.dataframe(db.table_sizes(), hide_index=True, use_container_width=True)
            st.caption("자동 수집이 매일 정리합니다: 우리 업종이 아닌 공고(딸린 자료 포함), 14일 지난 예비가격 15개 상세. "
                       "(예정가격·사정율은 남아서 분석에 지장 없음)")
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
        _clear_caches()
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
        _clear_caches()
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
    ca1, ca2 = st.columns(2)
    cabil = ca1.number_input("시공능력평가액(공시액, 원) — '시공능력 ○억 이상' 제한 공고 판단", 0.0, 1e13,
                             float(db.get_kv("company_ability", "") or 0), 1_000_000.0, format="%.0f")
    cperf = ca2.checkbox("실적제한 공고도 참가 가능으로 보기 (해당 실적이 있을 때만)",
                         value=db.get_kv("allow_perf_limit", "0") == "1",
                         help="끄면 실적경쟁·'실적 및 기술자 보유' 공고는 참가 가능 목록과 알림에서 뺍니다.")
    cjd = st.checkbox("지역의무공동도급 공고(다른 지역 현장)도 참가 가능으로 보기 — 그 지역 업체와 공동도급할 때만",
                      value=db.get_kv("allow_joint_duty", "0") == "1",
                      help="예: 부산 현장 + 지역의무공동도급 49% → 부산 업체와 손잡아야 참가 가능. 끄면 목록·알림에서 뺍니다. "
                           "우리 지역(울산) 현장은 항상 포함.")
    cpart = st.text_input("공동도급 협정으로 채울 수 있는 면허 (쉼표 구분, 예: 소방) — 전기+이 면허를 함께 요구하는 공고도 참가 가능으로",
                          db.get_kv("partner_licenses", None) if db.get_kv("partner_licenses", None) is not None else "소방")
    corg1, corg2 = st.columns(2)
    allow_o = corg1.text_input("참가 가능 기관 (쉼표 구분) — 소수 참가 기관이지만 우리가 등록돼 있어 넣을 수 있는 곳",
                               db.get_kv("allow_orgs", "") or "",
                               help="과거에 늘 30곳 이하만 들어온 기관(협력업체 등록 필요 등)은 자동으로 뺍니다. "
                                    "우리가 등록된 기관이면 여기에 이름 일부를 적으세요.")
    block_o = corg2.text_input("제외 기관 (쉼표 구분) — 우리가 못 넣는 기관", db.get_kv("block_orgs", "") or "")
    import kepco as _kp
    ksv = st.text_input("한전 용역 키워드 (쉼표 구분 — 이 단어가 공고명에 있는 한전 용역만 알림·분석)",
                        ", ".join(_kp.service_keywords()))
    if st.button("회사 정보 저장"):
        db.set_kv("kepco_service_keywords", ksv.strip())
        db.set_kv("company_ability", f"{cabil:.0f}" if cabil else "")
        db.set_kv("allow_perf_limit", "1" if cperf else "0")
        db.set_kv("allow_joint_duty", "1" if cjd else "0")
        db.set_kv("allow_orgs", allow_o.strip())
        db.set_kv("partner_licenses", cpart.strip())
        db.set_kv("block_orgs", block_o.strip())
        db.set_kv("qual_max_est", qlim.strip())
        db.set_kv("company_name", cname.strip())
        db.set_kv("company_bizno", cbiz.strip().replace("-", ""))
        db.set_kv("company_region", creg.strip())
        db.set_kv("company_licenses", clic.strip())
        _clear_caches()
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
        mgmt = q3.number_input("경영상태 한도(억원) — 유동비율·부채비율 만점이면 0", 0.0, 100000.0, _f("qual_mgmt"), 0.5)
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
                _clear_caches()
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
    cur_scope = notify.scope()
    reg_name = db.company()["region"] or "우리 지역"
    labels = {k: v.replace("울산", reg_name) for k, v in notify.SCOPES.items()}
    new_scope = st.radio("알림 받을 공고 범위", list(labels), index=list(labels).index(cur_scope),
                         format_func=labels.get, horizontal=True,
                         help="우리 지역: 참가지역에 우리 지역이 포함되거나 우리 지역 기관이 낸 공고. "
                              "전국: 지역제한 없는 다른 지역 공고까지 (개찰 결과 포함)")
    if new_scope != cur_scope:
        db.set_kv("notify_scope", new_scope)
        st.success(f"알림 범위를 '{labels[new_scope]}'(으)로 바꿨습니다. 다음 알림부터 적용됩니다.")
    st.caption(f"알림 조건: {labels[new_scope]} · 업종 {', '.join(kw_n)} · 지역 키워드 {', '.join(rg_n) or '없음'} · "
               f"{mx_n / 1e8:,.0f}억 미만 (위의 기본 키워드/금액 설정을 따릅니다)")
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
    with st.expander("🪖 국방전자조달(방위사업청) API 점검"):
        st.caption("공공데이터포털에서 '방위사업청_군수품조달정보 입찰공고_GW'와 '…입찰결과_GW'를 활용신청해야 합니다. "
                   "(나라장터와 같은 인증키 사용)")
        if st.button("국방 공고·결과 최근 7일 1건씩 보기"):
            import d2b
            for base_, op_, p_ in ((d2b.BID, "getFcltyCmpetBidPblancList",
                                    {"anmtDateBegin": d2b._d(date.today() - timedelta(days=7)), "anmtDateEnd": d2b._d(date.today())}),
                                   (d2b.RES, "getFcltyCmpetBidResultList",
                                    {"opengDateBegin": d2b._d(date.today() - timedelta(days=7)), "opengDateEnd": d2b._d(date.today())})):
                try:
                    items_, total_ = d2b.call(base_, op_, p_, rows=3)
                    st.success(f"{op_}: 전체 {total_:,}건")
                    if items_:
                        st.json(items_[0])
                except Exception as e:  # noqa: BLE001
                    st.error(f"{op_}: {e}")
        if st.button("10억 미만 국방 공고 상세 1건 보기 (지역제한 확인용)"):
            import d2b
            try:
                lst_, _ = d2b.call(d2b.BID, "getFcltyCmpetBidPblancList",
                                   {"anmtDateBegin": d2b._d(date.today() - timedelta(days=14)),
                                    "anmtDateEnd": d2b._d(date.today())}, rows=100)
                small = [x for x in lst_ if "공사" in str(x.get("busiDivs", "공사"))
                         and (d2b._num(x.get("baseAmnt")) or 0) < 1.1e9]
                if not small:
                    st.info("최근 14일 10억 미만 국방 공사 공고가 없습니다.")
                else:
                    x = small[0]
                    p_ = {k: x.get(k) for k in ("pblancYear", "pblancSeCode", "pblancNo", "pblancOdr", "cntrwkNo", "orntCode")}
                    det_ = (d2b.call(d2b.BID, "getFcltyCmpetBidPblancDetail", p_)[0] or [{}])[0]
                    st.write(f"**{x.get('cntrwkNm')}** · 기초 {d2b._num(x.get('baseAmnt')) or 0:,.0f}원")
                    st.write({k: det_.get(k) for k in ("areaLmttList", "lcnsLmttList", "lc", "estmPrce", "cntrctMth",
                                                       "scsbidLwltRt", "bidPartcptReqstClosDt")})
                    with st.expander("상세 전체"):
                        st.json(det_)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
        if st.button("국방 최근 30일 지금 수집"):
            import d2b
            with st.spinner("수집 중…"):
                try:
                    st.success(d2b.auto(st.write, quick=False))
                    _clear_caches()
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
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
