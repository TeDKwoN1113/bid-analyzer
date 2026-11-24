"""기본 점검: 코드를 올릴 때마다 GitHub 에서 자동 실행됩니다.

샘플(가상) 데이터로 계산·알림·화면이 오류 없이 동작하는지 확인합니다.
"""
import json
from datetime import date, timedelta
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


def test_notify_scope_local(df):
    d = pd.DataFrame({"allowed_regions": ["울산광역시", "", "", "부산광역시, 울산광역시"],
                      "region": ["", "서울특별시", "", ""], "org": ["", "서울시", "울산광역시 남구", ""],
                      "demand_org": ""})
    assert analysis.local_mask(d, "울산").tolist() == [True, False, True, True]
    db.set_kv("notify_scope", "all")
    _, k_all = notify.build(df)
    db.set_kv("notify_scope", "local")
    _, k_loc = notify.build(df)
    assert notify.scope() == "local" and len(k_loc) <= len(k_all)


def test_sticky_pick_and_history(df):
    r = upcoming(df).iloc[0]
    kw = analysis.bid_keywords(r)
    pk = analysis.pick_bid(df, r, kw)
    near = round(pk["사정율"] + 0.02, 2)        # 확률이 거의 같은 이전 추천 → 유지
    assert analysis.pick_bid(df, r, kw, prev=near)["사정율"] == near
    far = 98.0                                   # 확률이 훨씬 낮은 이전 추천 → 새 추천으로 교체
    assert analysis.pick_bid(df, r, kw, prev=far)["사정율"] == pk["사정율"]
    assert analysis.log_picks(df) > 0
    h = {"K": [("2026-11-21", 100.54, 1, 1), ("2026-11-22", 100.54, 1, 1), ("2026-11-23", 100.27, 1, 1)]}
    assert analysis.history_line(h, "K") == "11/21 100.54% → 11/23 100.27%"
    assert analysis.history_line(h, "X") is None


def test_pick_scorecard(df):
    done = df[df["sajung"].notna() & df["winner_sajung"].notna()].head(3)
    rows = []
    for (_, r), s_ in zip(done.iterrows(), [r_ - 1 for r_ in done["sajung"]]):
        rows.append({"bid_key": r["bid_key"], "day": (r["open_date"] - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                     "sajung": s_, "amount": 1.0, "prob": 1.0, "at": "x"})
    r0 = done.iloc[0]
    rows[0]["sajung"] = float((r0["sajung"] + r0["winner_sajung"]) / 2)   # 낙찰 구간 안 → 낙찰
    db.upsert_many("pick_log", rows)
    sc = analysis.pick_scorecard(df)
    res = dict(zip(sc["bid_key"], sc["결과"]))
    assert res[r0["bid_key"]] == "🏆 낙찰"
    assert res[done.iloc[1]["bid_key"]] == "하한 미달"
    assert "낙찰" in analysis.scorecard_summary(sc)
    text, keys = notify.build(df)
    assert isinstance(text, str)


def test_kepco_mapping_and_stats(df):
    import kepco
    rec = {"no": "E012512345", "companyId": "COM01", "name": "울산 배전선로 지중화 전기공사",
           "itemType": "Construction", "purchaseType": "ConstructionService", "placeName": "울산본부",
           "noticeDate": "20261120100000", "endDatetime": "20261201110000", "estimatedPriceBasicAmount": "1,234,000",
           "approximateAmount": "1235000", "bidType": "QualifiedEval", "progressState": "OpenTimed",
           "filenlink1": "http://srm.kepco.net/x", "filename1": "공고.pdf"}
    m = kepco.map_row(rec)
    assert m["bid_no"] == "E012512345" and m["base_amt"] == 1234000 and m["end_dt"] == "2026-12-01 11:00:00"
    t = pd.DataFrame([{"name": "배전공사 현장 안전감시 위탁용역", "item_type": None, "purchase_type": "ConstructionService"},
                      {"name": "울산 배전 전기공사", "item_type": "Construction", "purchase_type": "ConstructionService"}])
    assert kepco.is_construction(t).tolist() == [False, True]
    r = pd.DataFrame([{"name": "울산 배전공사", "place": "", "dept": "", "location": "", "restrict_text": "", "qualification": ""},
                      {"name": "배전공사", "place": "", "dept": "", "location": "", "restrict_text": "전기공사업 등록업체", "qualification": ""},
                      {"name": "배전공사", "place": "", "dept": "", "location": "", "restrict_text": "경기도 소재 업체", "qualification": ""}])
    assert kepco.region_scope(r, "울산").tolist() == ["local", "national", "other"]
    kb = kepco.load()
    assert len(kb) >= 150 and kb["sajung"].between(95, 105).mean() > 0.9
    loc = kepco.filter_bids(kb, region="울산", local_only=True)
    assert len(loc) > 0
    st_ = kepco.sajung_stats(kb, loc.iloc[0], "울산")
    assert st_ and 98 < st_["중앙"] < 102
    text, keys = notify.build(df)
    assert any(k.startswith("KEPCO-") for k, _ in keys)
    # 직접 기록한 개찰 결과 → 사정율·1순위 사정율 → 추천
    past = kb[kb["end_dt"] < pd.Timestamp.now()].head(15)
    rows = []
    for i, (_, r) in enumerate(past.iterrows()):
        plan = r["base_amt"] * (0.995 + 0.0007 * i)
        rows.append({"bid_no": r["bid_no"], "plan_price": plan, "first_amt": plan * 0.88, "lower_rate": 87.745,
                     "a_value": 0, "participants": 50, "created_at": "x"})
    db.upsert_many("kepco_results", rows)
    kb2 = kepco.load()
    one = kb2.set_index("bid_no").loc[rows[0]["bid_no"]]
    assert abs(one["sajung"] - 99.5) < 0.01 and one["winner_sajung"] > one["sajung"]
    rec = kepco.recommend(kb2, None)
    assert rec and 98 < rec["사정율"] < 102
    # SRM 이력 엑셀 불러오기 (칸 이름 추측 + 목록에 없는 공고는 최소 정보로 추가)
    xl = pd.DataFrame({"공고번호": ["SRM-TEST-1", "SRM-TEST-2", ""], "입찰건명": ["배전공사A", "배전공사B", "x"],
                       "예비가격기초금액": ["100,000,000", "200,000,000", "1"], "예정가격": ["99,800,000", "201,000,000", ""],
                       "1순위금액": ["87,700,000", "176,500,000", ""], "투찰금액": ["88,000,000", None, None]})
    mp = kepco.guess_columns(list(xl.columns))
    assert mp["bid_no"] == "공고번호" and mp["plan_price"] == "예정가격" and mp["first_amt"] == "1순위금액"
    n_ok, n_skip = kepco.import_results(xl, mp)
    assert n_ok == 2 and n_skip == 1
    kb3 = kepco.load().set_index("bid_no")
    assert abs(kb3.at["SRM-TEST-1", "sajung"] - 99.8) < 1e-6
    # SRM '개찰결과' 화면을 통째로 복사한 글 읽기
    bno = kb[kb["end_dt"] < pd.Timestamp.now()].iloc[-1]["bid_no"]
    txt = ("입찰공고 개찰결과\n공고번호\tE012608144\t공고명\t매암동 조사용역\n"
           "예정가격\t102,160,664\t원\t적격률(%)\t89.745\n※ 적격심사 대상자 목록\n"
           "심사순위\t업체명\t사업자등록번호\t대표자\t투찰금액\t투찰율(%)\t투찰일시\n"
           "1번\t미라테크\t506-34-71764\t노경희\t91,853,705\t89.911\t2026/11/20 09:33\n"
           "2번\t주식회사 한...\t617-81-14903\t김창윤\t91,984,000\t90.039\t2026/11/23 09:18\n"
           "※ 적격률 탈락(미만, 초과) 대상자 목록\n업체명\t사업자등록번호\t대표자\t투찰금액\t투찰율(%)\n"
           "가나\t123-45-67890\t홍길동\t90,000,000\t88.097\n").replace("E012608144", bno)
    db.execute("UPDATE kepco_bids SET base_amt = 102300000 WHERE bid_no = :n", {"n": bno})
    pr = kepco.parse_result_text(txt)
    assert pr["bid_no"] == bno and pr["plan_price"] == 102160664 and pr["lower_rate"] == 89.745
    assert [b["rank"] for b in pr["bidders"]] == [1, 2] and len(pr["below"]) == 1
    two = kepco.parse_many(txt + "\n\n" + txt.replace(bno, "E099999999").replace("102,160,664", "200,000,000"))
    assert [x["bid_no"] for x in two] == [bno, "E099999999"] and two[1]["plan_price"] == 200000000
    msg = kepco.save_parsed_result(pr, "1234567890")
    assert "참가 3곳" in msg and "우리 90,000,000원" in msg
    one2 = kepco.load().set_index("bid_no").loc[bno]
    assert one2["participants"] == 3 and one2["winner_sajung"] > 0


def test_plan_price_fallback_from_rate(df):
    done = df[df["plan_price"].notna() & df["first_rate"].notna()].head(5)
    before = dict(zip(done["bid_key"], done["sajung"]))
    for k in before:
        db.execute("DELETE FROM plans WHERE bid_key = :k", {"k": k})
    d2 = analysis.load_dataset().set_index("bid_key")
    for k, sj in before.items():
        assert d2.at[k, "plan_source"] == "투찰률 역산"
        assert abs(d2.at[k, "sajung"] - sj) < 0.002     # 소수 3자리 투찰률 → 사정율 오차 아주 작음


def test_d2b_mapping_and_flow(df, monkeypatch):
    import d2b
    xml = ("<response><header><resultCode>00</resultCode></header><body><items><item><pblancNo>2026-18396</pblancNo>"
           "<cntrwkNm>울산 부대 전기공사</cntrwkNm></item></items><totalCount>1</totalCount></body></response>")
    items, total = d2b._items_from_xml(xml)
    assert total == 1 and items[0]["cntrwkNm"] == "울산 부대 전기공사"
    soon = (pd.Timestamp.now() + pd.Timedelta(days=3)).strftime("%Y%m%d%H%M")
    lst = {"pblancYear": "2026", "pblancSeCode": "1", "pblancNo": "2026-18396", "pblancOdr": "0", "cntrwkNo": "C1",
           "orntCode": "9001", "cntrwkNm": "울산 OO부대 전기공사", "ornt": "국군재정관리단", "busiDivs": "공사",
           "pblancDate": "20261120", "opengDt": soon, "biddocPresentnClosDt": soon, "baseAmnt": "300,000,000",
           "g2bPblancNo": "2026HFD00222026-18306"}   # 방위사업청 자체 번호 (나라장터 번호 아님)
    det = {"areaLmttList": "울산광역시", "lcnsLmttList": "전기공사업", "scsbidLwltRt": "87.745", "estmPrce": "272727272",
           "inanAmnt": "1000000", "inheAmnt": "800000", "safeAmnt": "2000000", "cntrwkSe": "전기"}
    calls = []

    def fake_call(base, op, params, page=1, rows=100):
        calls.append(op)
        if op == "getFcltyCmpetBidPblancList":
            return [lst], 1
        if op == "getFcltyCmpetBidPblancDetail":
            return [det], 1
        return [], 0
    monkeypatch.setattr(d2b, "call", fake_call)
    n, saved = d2b.collect_notices(date.today() - timedelta(days=5), date.today())
    assert saved == 1
    d = analysis.load_dataset().set_index("bid_key")
    k = "D2B9001_202618396-000"
    assert k in d.index and d.at[k, "allowed_regions"] == "울산광역시" and d.at[k, "a_value"] == 3800000
    assert analysis.eligible(d.loc[[k]]).iloc[0]
    # 10억 미만인데 지역제한 목록이 비어 있으면 '제한없음'이 아니라 공사 위치로 판단
    db.execute("DELETE FROM regions WHERE bid_key = :k", {"k": k})
    db.execute("UPDATE bids SET region = '경기도 포천시', org = '육군 OO사단' WHERE bid_key = :k", {"k": k})
    d3 = analysis.load_dataset().set_index("bid_key")
    assert not d3.at[k, "allowed_regions_known"] and not analysis.eligible(d3.loc[[k]]).iloc[0]
    db.execute("UPDATE bids SET region = '울산광역시 북구' WHERE bid_key = :k", {"k": k})
    d4 = analysis.load_dataset().set_index("bid_key")
    assert analysis.eligible(d4.loc[[k]]).iloc[0]
    db.upsert_many("regions", [{"bid_key": k, "region": "울산광역시"}])
    pk = analysis.pick_bid(df, d.loc[k], analysis.bid_keywords(d.loc[k]))
    assert pk and pk["투찰금액"] > 0
    # 결과: 선택된 복수예비가격 평균 = 예정가격, 1순위·낙찰자
    pre = [{"epprRank": str(i), "planPrce": str(300_000_000 + i * 100_000), "choiYsno": "Y" if i in (1, 2, 3, 4) else "N"}
           for i in range(1, 16)]
    bidders = [{"bidnRank": "1", "tbidAmnt": "265000000", "bznsRgnb": "111-11-11111", "mfkrName": "가", "bidnRate": "88.1"},
               {"bidnRank": "2", "tbidAmnt": "266000000", "bznsRgnb": "222-22-22222", "mfkrName": "나", "bidnRate": "88.4"}]
    tb = d2b.map_result({**lst, "opengDate": "20261123"}, {"bsisPreparPc": "300000000", "scsbidEntrpsBsnmRegistNo": "1111111111",
                        "scsbidEntrpsNm": "가"}, pre, bidders)
    assert tb["plans"][0]["plan_price"] == 300_250_000
    assert not d2b.on_g2b("2026HFD00222026-18306") and d2b.on_g2b(df["bid_no"].iloc[0])
    assert tb["openings"][0]["first_amt"] == 265000000 and tb["results"][0]["win_amt"] == 265000000
    assert len(tb["ranks"]) == 2


def test_comparable_bids_prevent_high_bias(df):
    """소수 참가 공고(이기는 구간이 넓음)가 섞여도 많이 들어오는 공고의 추천이 위로 끌려가지 않아야 함."""
    r = upcoming(df).iloc[0]
    kw = analysis.bid_keywords(r)
    base_pick = analysis.pick_bid(df, r, kw)["사정율"]
    done = df[df["sajung"].notna()]
    fake = done.sample(int(len(done) * 0.15), replace=True, random_state=1).copy()   # 실제처럼 일부만
    fake["bid_key"] = [f"FAKE{i}-000" for i in range(len(fake))]
    fake["bid_no"] = [f"FAKE{i}" for i in range(len(fake))]
    fake["participants"] = 5
    fake["sajung"] = 99.6
    fake["winner_sajung"] = 101.8      # 넓은 낙찰 구간 → 높은 사정율 쪽 확률을 부풀림
    biased = pd.concat([df, fake], ignore_index=True)
    analysis._ctx_cache.clear()
    pick2 = analysis.pick_bid(biased, r, kw)["사정율"]
    assert abs(pick2 - base_pick) < 0.15, (base_pick, pick2)
    # 예전 방식(참가업체 수 구분 없음)이었다면 위로 끌려갔는지 확인 → 이 테스트가 실제 문제를 잡는지 검증
    old = analysis.COMPARABLE_RATIO
    analysis.COMPARABLE_RATIO = 0
    analysis._ctx_cache.clear()
    try:
        pick_old = analysis.pick_bid(biased, r, kw)["사정율"]
    finally:
        analysis.COMPARABLE_RATIO = old
        analysis._ctx_cache.clear()
    assert pick_old - base_pick > 0.15, (base_pick, pick_old)


def test_our_actual_bid_found_from_ranks(df, monkeypatch):
    """추천 금액으로 기록했어도, 개찰 후 개찰순위에서 우리 사업자번호의 실제 금액·순위로 바뀌어야 함."""
    db.set_kv("company_bizno", "123-45-67890")
    done = df[df["participants"] > 0].sort_values("open_date").iloc[-1]
    key = done["bid_key"]
    db.upsert_many("my_bids", [{"bid_key": key, "amount": 100.0, "sajung": 100.5, "memo": "", "my_rank": None,
                                "checked_at": None, "created_at": "x", "rec_amount": 100.0,
                                "amount_source": "추천 금액으로 기록"}])
    items = [{"opengRank": str(i), "prcbdrBizno": f"9{i:09d}", "prcbdrNm": f"업체{i}", "bidprcAmt": str(1000 + i)}
             for i in range(1, 200)]
    items[149]["prcbdrBizno"] = "1234567890"          # 우리 회사 150위
    monkeypatch.setattr(collector.api_client, "call_all", lambda op, params, max_pages=200: items)
    collector.update_my_bids(scan_limit=0)
    m = db.query_df("SELECT * FROM my_bids WHERE bid_key = :k", {"k": key}).iloc[0]
    assert m["my_rank"] == 150 and m["amount"] == 1150 and m["rec_amount"] == 100
    assert m["amount_source"] == "개찰순위(실제)" and pd.isna(m["sajung"])
    db.execute("DELETE FROM my_bids WHERE bid_key = :k", {"k": key})


def test_limit_reasons_exclude_unenterable_bids():
    d = pd.DataFrame({"name": ["[★기술자 및 실적보유★경부고속선 광명~동대구간 지장물검지장치 개량공사",
                               "[★전차선실적및기술자보유★부산기지선 부산철도차량정비단(고속) 동력단로기 개량공사",
                               "울산 남구 가로등 정비 전기공사", "학교 수변전 전기공사", "청사 전기공사", "도로 신호등 전기공사"],
                      "perf_limit": [None, None, "N", "Y", "N", "N"], "pq": [None, None, None, None, "Y", None],
                      "designated": [None] * 6,
                      "ability_list": [None, None, None, None, None, "전기공사업 시공능력평가액 2,000,000,000원 이상"]})
    db.set_kv("company_ability", "1262577000")
    r = analysis.limit_reasons(d).tolist()
    assert r[0] == "실적제한" and r[1] == "실적제한" and r[2] == "" and r[3] == "실적제한"
    assert r[4] == "PQ심사" and r[5] == "시공능력 부족"
    db.set_kv("allow_perf_limit", "1")          # 실적 있는 회사는 허용 가능
    assert analysis.limit_reasons(d).tolist()[0] == ""
    db.set_kv("allow_perf_limit", "0")


def test_modoobid_history_import():
    """모두부드 투찰 이력 엑셀 형식: 칸 추측, 공고번호 차수 떼기, 하한율·A값 역산(화면 낙찰하한가와 일치)."""
    import kepco
    x = pd.DataFrame({"번호": [1, 2], "공고명": ["'27년 부산울산본부 울산지사 저압 협력회사 C지역 공사", "2026년 평창지사 콘크리트전주 내부진단용역"],
                      "발주처": ["한국전력공사"] * 2, "공고번호": ["E012607755-01", "E012603540-01"], "지역": ["울산", "전국"],
                      "업종/품명": ["전기", "기술사사무소/엔지니어링(설비)"], "예정가격": [85247039, 47852486],
                      "기초금액": [84093067, 47943296], "예가/기초(100%)": [101.3723, 99.8106],
                      "자사사정율(100%)": [100.1329, 100.2452], "자사순위": ["-65위", "67위"], "업체수": [174, 183],
                      "자사투찰금액": [75249956, 42171016], "1순위 업체명": ["대성이텍", "(주)오성이엠"],
                      "1순위투찰금액": [76327440, 41989600], "실개찰일시": ["2026-11-11 18:14:00", "2026-05-15 15:16:28"]})
    mp = kepco.guess_columns(list(x.columns))
    assert mp["first_amt"] == "1순위투찰금액" and mp["our_amount"] == "자사투찰금액" and mp["our_rank"] == "자사순위"
    assert mp["plan_price"] == "예정가격" and mp["base_amt"] == "기초금액"
    lr, a = kepco.infer_rate_and_a(84093067, 75249956, 100.1329)
    assert lr == 88.745 and abs(round((85247039 - a) * lr / 100 + a) - 76174867) <= 2   # 화면의 낙찰하한가
    assert kepco.infer_rate_and_a(47943296, 42171016, 100.2452)[0] == 87.745
    n, sk = kepco.import_results(x, mp)
    kb = kepco.load().set_index("bid_no")
    assert n == 2 and "E012607755" in kb.index and kb.at["E012603540", "item_type"] == "Service"
    assert kb.at["E012603540", "winner_sajung"] > kb.at["E012603540", "sajung"]
    assert len(kepco.filter_bids(kb.reset_index(), kind="용역")) >= 1


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
    d2 = d.assign(method=["수의시담-수의시담", "", ""])
    assert not analysis.eligible(d2, "울산", ["전기공사업"]).iloc[0]


PAGES = ["🏠 오늘의 투찰", "📋 공고 현황", "📊 사정율 분석", "🧮 투찰 계산기", "📝 우리 투찰",
         "🏢 경쟁업체", "🔌 한전 입찰", "🔬 추천 검증", "📖 사용법", "⬇️ 데이터 수집", "⚙️ 설정 · API 점검"]


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
    """참가등록 마감 알림: 나라장터는 없음, 국방·한전은 전날/당일."""
    db.use_sample(True)
    now = pd.Timestamp.now()
    tomorrow = (now.normalize() + pd.Timedelta(days=1, hours=10)).strftime("%Y-%m-%d %H:%M:%S")
    k = upcoming(df).iloc[0]["bid_key"]
    db.execute("UPDATE bids SET qual_dt = :q WHERE bid_key = :k", {"q": tomorrow, "k": k})
    # 같은 공고를 국방 공고로 복제
    row = db.query_df("SELECT * FROM bids WHERE bid_key = :k", {"k": k}).iloc[0].to_dict()
    row.update({"bid_key": "D2BTEST_1-000", "bid_no": "D2BTEST_1", "bid_ord": "000"})
    db.upsert_many("bids", [row])
    for t in ("regions", "licenses"):
        db.upsert_many(t, [{"bid_key": "D2BTEST_1-000", ("region" if t == "regions" else "license"):
                            ("울산광역시" if t == "regions" else "전기공사업")}])
    kb = db.query_df("SELECT bid_no FROM kepco_bids").iloc[-1]["bid_no"]
    db.execute("UPDATE kepco_bids SET attend_close_dt = :q, end_dt = :e WHERE bid_no = :n",
               {"q": tomorrow, "e": (now + pd.Timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), "n": kb})
    text, keys = notify.build(analysis.load_dataset())
    assert (k, "qual1") not in keys and (k, "qual0") not in keys          # 나라장터: 참가등록 알림 없음
    assert ("D2BTEST_1-000", "qual1") in keys                              # 국방: 전날 알림
    assert ("KEPCO-" + kb, "qual1") in keys                                # 한전: 전날 알림
    assert "입찰참가등록 마감 임박" in text

def test_weekly_report(df):
    db.use_sample(True)
    text = notify.weekly_report(analysis.load_dataset(), days=60)
    assert "주간 성적" in text


def test_likely_competitors(df):
    db.use_sample(True)
    ranks = analysis.load_ranks()
    lab, comp = analysis.likely_competitors(ranks, upcoming(df).iloc[0], df)
    assert lab and not comp.empty and comp["등장"].iloc[0] >= comp["등장"].iloc[-1]


def test_my_bids_view_with_empty_numeric_columns(df, monkeypatch):
    """자동 발견한 우리 투찰(금액·사정율이 모두 비어 글자형으로 읽힘)이 있어도 알림이 깨지지 않아야 함."""
    key = df["bid_key"].iloc[0]
    fake = pd.DataFrame({"bid_key": [key], "amount": [None], "sajung": [None], "memo": [None],
                         "my_rank": [None], "checked_at": [None], "created_at": [None],
                         "rec_amount": [None], "amount_source": [None]}, dtype=object)
    real = db.query_df
    monkeypatch.setattr(db, "query_df", lambda sql, p=None: fake.copy() if sql.strip() == "SELECT * FROM my_bids" else real(sql, p))
    v = analysis.my_bids_view(df)
    assert len(v) == 1


def test_orphan_rows_pruned_and_not_inserted(df):
    """지운 공고의 개찰결과·예비가격은 다시 쌓이지 않고, 남아 있던 것도 정리됨."""
    db.upsert_many("openings", [{"bid_key": "ZZORPHAN01-000", "participants": 5}])
    db.upsert_many("prelim_prices", [{"bid_key": "ZZORPHAN01-000", "sno": 1, "price": 1.0, "drawn": 0}])
    db.upsert_many("ranks", [{"bid_key": "KEPCO-K1", "rank": 1, "bizno": "1"}])
    kept = df["bid_key"].iloc[0]
    n_before = len(db.query_df("SELECT * FROM openings WHERE bid_key = :k", {"k": kept}))
    assert db.prune_orphans() >= 2
    assert db.query_df("SELECT * FROM openings WHERE bid_key = 'ZZORPHAN01-000'").empty
    assert db.query_df("SELECT * FROM prelim_prices WHERE bid_key = 'ZZORPHAN01-000'").empty
    assert len(db.query_df("SELECT * FROM ranks WHERE bid_key = 'KEPCO-K1'")) == 1  # 한전은 유지
    assert len(db.query_df("SELECT * FROM openings WHERE bid_key = :k", {"k": kept})) == n_before

    # 수집 단계에서 주인 없는 행은 저장하지 않음
    import api_client
    items = [{"bidNtceNo": "ZZORPHAN02", "bidNtceOrd": "000", "prtcptCnum": "3"},
             {"bidNtceNo": kept.split("-")[0], "bidNtceOrd": kept.split("-")[1], "prtcptCnum": "3"}]
    orig = api_client.call_all
    api_client.call_all = lambda op, params: items
    try:
        collector._KNOWN["nos"] = None
        collector.collect_day("openings", collector.map_opening, date(2026, 11, 21))
    finally:
        api_client.call_all = orig
    assert db.query_df("SELECT * FROM openings WHERE bid_key LIKE 'ZZORPHAN02%'").empty


@pytest.mark.parametrize("mode", ["🧮 투찰금액 계산", "📥 개찰결과·투찰 이력 넣기"])
def test_kepco_page_modes_render(df, mode):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.run()
    at.sidebar.radio[0].set_value("🔌 한전 입찰").run()
    r = [x for x in at.radio if x.key == "kepco_mode"]
    assert r
    r[0].set_value(mode).run()
    assert not at.exception, [e.value for e in at.exception]
    kinds = [x for x in at.radio if x.key == "kc_kind"]
    for i in range(len(kinds[0].options) if kinds else 0):
        x = [y for y in at.radio if y.key == "kc_kind"][0]
        x.set_value(x.options[i]).run()
        assert not at.exception, [e.value for e in at.exception]


def test_license_groups_need_all_in_group():
    """같은 묶음의 면허는 모두 있어야 함 (예: 승강기 교체공사 = 전기 + 승강기공사업 + 승강기 제조업 …)."""
    r = {"bidNtceNo": "R26BK01818339", "bidNtceOrd": "000", "lmtGrpNo": "1", "lcnsLmtNm": "승강기 제조업/6876",
         "permsnIndstrytyList": "", "bsnsDivNm": "공사"}
    rows = collector.map_license(r)
    assert rows and rows[0][1]["license"].startswith("[G1] ")
    elevator = (("승강기 유지관리업(중저속 승강기)/6881 [승강기 유지관리업(고속 승강기)/6880]", "승강기 제조업/6876",
                 "승강기ㆍ삭도공사업/6201", "전기공사업/0037"),)
    assert not analysis.license_ok(elevator, ["전기공사업"])
    assert analysis.license_ok((("전기공사업/0037",), ("정보통신공사업/0036",)), ["전기공사업"])
    assert analysis.license_ok((("정보통신공사업/0036 [전기공사업/0037]",),), ["전기공사업"])
    assert not analysis.license_ok((("정보통신공사업/0036",),), ["전기공사업"])
    df = pd.DataFrame({"bid_key": ["A-000", "B-000"], "licenses": ["x", "y"], "license_groups": [elevator, (("전기공사업/0037",),)],
                       "allowed_regions": ["", ""], "allowed_regions_known": [True, True], "region": ["", ""], "org": ["", ""],
                       "demand_org": ["", ""], "method": ["", ""], "name": ["승강기 교체", "전기공사"]})
    ok = analysis.eligible(df, company_region="울산", company_licenses=["전기공사업"])
    assert list(ok) == [False, True]


def test_refresh_my_results_fills_rank_and_plan(df, monkeypatch):
    """개찰 시간이 지난 우리 투찰은 공고번호로 바로 조회해 순위·실제 금액·예정가격을 채움."""
    import api_client
    past = df[df["open_date"] < pd.Timestamp.now()].iloc[0]
    key = past["bid_key"]
    no, ord_ = key.rsplit("-", 1)
    db.execute("DELETE FROM plans WHERE bid_key = :k", {"k": key})
    db.upsert_many("my_bids", [{"bid_key": key, "amount": None, "sajung": None, "memo": "t", "my_rank": None,
                                "checked_at": None, "created_at": db.now(), "rec_amount": 123.0, "amount_source": None}])
    monkeypatch.setattr(db, "company", lambda: {"bizno": "1112233333", "region": "울산", "licenses": ["전기공사업"]})

    def fake(op, params, max_pages=200):
        if op == "ranks":
            return [{"opengRank": "1", "prcbdrBizno": "999", "prcbdrNm": "가", "bidprcAmt": "1000"},
                    {"opengRank": "2", "prcbdrBizno": "111-22-33333", "prcbdrNm": "우리", "bidprcAmt": "1001"}]
        return [{"bidNtceNo": no, "bidNtceOrd": ord_, "compnoRsrvtnPrceSno": "1", "bsisPlnprc": "10",
                 "drwtYn": "Y", "plnprc": "1050", "bssamt": "1000", "rlOpengDt": past["open_dt"]}]
    monkeypatch.setattr(api_client, "call_all", fake)
    msg = collector.refresh_my_results(days=3650)
    assert "실제 금액 찾음 1건" in msg, msg
    m = db.query_df("SELECT * FROM my_bids WHERE bid_key = :k", {"k": key}).iloc[0]
    assert int(m["my_rank"]) == 2 and float(m["amount"]) == 1001
    assert len(db.query_df("SELECT * FROM plans WHERE bid_key = :k", {"k": key})) == 1
    db.execute("DELETE FROM my_bids WHERE bid_key = :k", {"k": key})


def test_regional_joint_contract_excluded_outside_our_region(monkeypatch):
    """지역의무공동도급(예: 부산 현장 49%)은 다른 지역이면 제외, 우리 지역 현장이면 포함."""
    monkeypatch.setattr(db, "company", lambda: {"bizno": "", "region": "울산", "licenses": ["전기공사업"]})
    d = pd.DataFrame({"name": ["청룡초 전기공사", "울산 학교 전기공사", "부산 일반"], "joint_duty": ["Y", "Y", "N"],
                      "region": ["부산광역시", "울산광역시 남구", "부산광역시"]})
    r = analysis.limit_reasons(d)
    assert "지역의무공동도급" in r.iloc[0] and r.iloc[1] == "" and r.iloc[2] == ""


def test_parse_single_item_body_and_d2b_lists():
    """국방 상세처럼 body.item 에 한 건이 바로 오는 응답도 읽어야 함 (예전엔 빈칸으로 저장됐음)."""
    import json as _j

    import api_client
    import d2b

    class R:
        def __init__(self, t):
            self.text = t

        def json(self):
            return _j.loads(self.text)
    items, _ = api_client._parse(R('{"response":{"header":{"resultCode":"00"},"body":{"item":'
                                   '{"areaLmttList":"[19] 충청남도","scsbidLwltRt":89.745}}}}'))
    assert items and items[0]["areaLmttList"] == "[19] 충청남도"
    assert d2b._split_list("[6202] 기계설비,가스공사업") == ["[6202] 기계설비,가스공사업"]
    assert d2b._split_list("[19] 충청남도, [26] 부산광역시") == ["[19] 충청남도", "[26] 부산광역시"]
    tb = d2b.map_notice({"pblancNo": "X1", "orntCode": "UMM", "pblancOdr": 1, "baseAmnt": 2.5e8, "cntrwkNm": "전기공사"},
                        items[0])
    assert tb["regions"][0]["region"] == "[19] 충청남도" and tb["bids"][0]["lower_rate"] == 89.745


def test_small_participation_orgs_excluded(monkeypatch):
    """늘 소수만 참가하는 기관(협력업체 등록 등)은 못 넣는 공고로 제외. 설정으로 허용 가능."""
    monkeypatch.setattr(db, "company", lambda: {"bizno": "", "region": "울산", "licenses": ["전기공사업"]})
    d = pd.DataFrame({"name": ["a", "b"], "org": ["중소기업은행", "울산광역시"], "org_part_median": [11, 350],
                      "org_part_n": [20, 50]})
    r = analysis.limit_reasons(d)
    assert "소수 참가 기관" in r.iloc[0] and r.iloc[1] == ""
    db.set_kv("allow_orgs", "기업은행")
    try:
        assert analysis.limit_reasons(d).iloc[0] == ""
    finally:
        db.set_kv("allow_orgs", "")
