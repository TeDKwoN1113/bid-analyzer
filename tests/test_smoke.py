"""기본 점검: 코드를 올릴 때마다 GitHub 에서 자동 실행됩니다.

샘플(가상) 데이터로 계산·알림·화면이 오류 없이 동작하는지 확인합니다.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for k in ("DATABASE_URL", "DATA_GO_KR_KEY", "APP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(k, None)

import analysis  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402
import notify  # noqa: E402
import sample_data  # noqa: E402
import tune  # noqa: E402


@pytest.fixture(scope="module")
def df():
    sample_data.build()
    db.use_sample(True)
    return analysis.load_dataset()


def upcoming(df):
    return df[df["plan_price"].isna() & df["base_amt"].notna()]


def test_dataset_has_sajung(df):
    assert len(df) > 100
    assert df["sajung"].between(95, 105).mean() > 0.9
    assert df["winner_sajung"].notna().sum() > 100


def test_bid_amount_formula():
    # (예정가격 - A) × 하한율 + A, 원 단위 올림
    assert analysis.bid_amount(1_000_000_000, 100, 87.745, 0) == 877_450_000
    assert analysis.bid_amount(1_000_000_000, 100, 87.745, 30_000_000) == int(np.ceil((970_000_000) * 0.87745 + 30_000_000))


def test_win_curve_matches_bruteforce():
    rng = np.random.default_rng(0)
    a = rng.normal(100, 0.6, 500)
    h = pd.DataFrame({"bid_key": range(500), "sajung": a, "winner_sajung": a + np.abs(rng.normal(0, 0.2, 500))})
    c = analysis.win_curve(h)
    g = c["사정율"].values[:, None]
    brute = ((g >= a[None, :]) & (g < h["winner_sajung"].values[None, :])).mean(axis=1) * 100
    assert np.allclose(c["낙찰확률(원값)"].values, brute.round(2))
    assert analysis.n_windows(h) == 500


@pytest.mark.parametrize("mode", list(analysis.STRATEGIES))
def test_pick_bid_all_modes(df, mode):
    r = upcoming(df).iloc[0]
    pk = analysis.pick_bid(df, r, ["전기"], mode=mode)
    assert pk and pk["투찰금액"] > 0 and 90 < pk["사정율"] < 110
    assert analysis.evidence_lines(pk)


def test_pure_cost_floor(df):
    r = upcoming(df).iloc[0].copy()
    r["pure_cost"] = r["base_amt"] * 0.95
    pk = analysis.pick_bid(df, r, ["전기"])
    assert pk["투찰금액"] >= pk["순공사원가 하한"]


def test_map_base_a_value():
    item = {"bidNtceNo": "R1", "bidNtceOrd": "000", "bssamt": "3274230000", "sftyMngcst": "62844003",
            "sftyChckMngcst": "16120000", "rtrfundNon": "21471076", "mrfnHealthInsrprm": "33560227",
            "npnInsrprm": "44342443", "odsnLngtrmrcprInsrprm": "4409812", "bidPrceCalclAYn": "Y",
            "bssAmtPurcnstcst": "2693325515", "qltyMngcst": "30470000", "qltyMngcstAObjYn": "Y"}
    row = collector.map_base(item)[0][1]
    assert row["a_value"] == 213_217_561 and row["pure_cost"] == 2_693_325_515
    assert collector.map_base(dict(item, qltyMngcstAObjYn="N"))[0][1]["a_value"] == 213_217_561 - 30_470_000
    assert collector.map_base(dict(item, bidPrceCalclAYn="N"))[0][1]["a_value"] is None


def test_map_opening_corp_info():
    row = collector.map_opening({"bidNtceNo": "R1", "bidNtceOrd": "000", "prtcptCnum": "1129",
                                 "opengCorpInfo": "(주)대주지에스^1298183091^임금비^359486506^90.099"})[0][1]
    assert row["first_company"] == "(주)대주지에스" and row["first_amt"] == 359486506 and row["participants"] == 1129


def test_notify_build_and_my_bids(df):
    text, keys = notify.build(df)
    assert isinstance(text, str)
    v = analysis.my_bids_view(df)
    assert v is not None


def test_backtest_and_tune_rules(df):
    s, d = analysis.backtest(df, ["전기"], test_days=120, max_tests=20)
    assert not s.empty and set(s["방식"]) <= set(analysis.STRATEGIES.values())
    cur, alt = analysis.STRATEGIES["blend"], analysis.STRATEGIES["region_only"]
    mk = lambda a, b: pd.DataFrame([  # noqa: E731
        {"방식": cur, "시험 공고": 200, "순위 비교 공고": 80, "10위 이내": a, "10위 이내율(%)": a / 0.8, "1순위": 1, "무작위 대비(배)": 1},
        {"방식": alt, "시험 공고": 200, "순위 비교 공고": 80, "10위 이내": b, "10위 이내율(%)": b / 0.8, "1순위": 2, "무작위 대비(배)": 2}])
    assert tune.decide(mk(10, 20), cur)[0] == alt          # 확실히 좋으면 교체
    assert tune.decide(mk(10, 13), cur)[0] == cur          # 차이가 작으면 유지


def test_eligibility_regions():
    d = pd.DataFrame({"allowed_regions": ["울산광역시", "부산광역시", ""], "allowed_regions_known": [True, True, True],
                      "licenses": ["전기공사업/0036", "", "정보통신공사업/0037"], "region": "", "org": "", "demand_org": ""})
    ok = analysis.eligible(d, "울산", ["전기공사업"])
    assert ok.tolist() == [True, False, False]


PAGES = ["🏠 오늘의 투찰", "📋 공고 현황", "📊 사정율 분석", "🧮 투찰 계산기", "📝 우리 투찰",
         "🏢 경쟁업체", "🔬 추천 검증", "⬇️ 데이터 수집", "⚙️ 설정 · API 점검"]


@pytest.mark.parametrize("page", PAGES)
def test_app_pages_render(df, page):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_revised_and_cancelled_notices():
    d = pd.DataFrame({"bid_key": ["A-000", "A-001", "B-000", "C-000"], "bid_no": ["A", "A", "B", "C"],
                      "bid_ord": ["000", "001", "000", "000"], "close_dt": ["11/20", "11/25", "x", "y"],
                      "base_amt_notice": [100.0, None, 5.0, 7.0], "plan_price": [None, 99.0, None, None],
                      "ntce_kind": ["등록공고", "변경공고", "등록공고", "취소공고"]})
    out = analysis._latest_order(d).set_index("bid_no")
    assert list(sorted(out.index)) == ["A", "B"]                       # 취소공고 제외
    assert out.at["A", "bid_ord"] == "001" and out.at["A", "close_dt"] == "11/25"  # 최신 차수
    assert out.at["A", "base_amt_notice"] == 100.0                     # 이전 차수 값으로 채움


def test_qualification_deadline_reminder(df):
    db.use_sample(True)
    k = upcoming(df).iloc[0]["bid_key"]
    soon = (pd.Timestamp.now() + pd.Timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE bids SET qual_dt = :q WHERE bid_key = :k", {"q": soon, "k": k})
    text, keys = notify.build(analysis.load_dataset())
    assert (k, "qual") in keys and "참가신청 마감" in text


def test_weekly_report(df):
    db.use_sample(True)
    text = notify.weekly_report(analysis.load_dataset(), days=60)
    assert "주간 성적" in text
