"""연습용 가상 데이터 생성 (실제 데이터 아님).

API 연결 전에 화면과 분석 기능을 미리 써볼 수 있도록 data/sample.db 를 만듭니다.
"""
import os
import random
from datetime import datetime, timedelta

import numpy as np

import db
from config import SAMPLE_DB_PATH

ORGS = [
    "울산광역시", "울산광역시 남구", "울산광역시 중구", "울산광역시 울주군", "울산광역시교육청",
    "한국토지주택공사 울산울산권주거복지지사", "부산지방조달청", "경상남도 양산시", "부산광역시 기장군",
    "한국수자원공사", "조달청 본청",
]
REGIONS = ["울산광역시", "울산광역시", "울산광역시", "부산광역시", "경상남도", "전국"]
WORKS = ["가로등 정비 전기공사", "학교 수변전설비 교체 전기공사", "공동주택 전기공사", "LED 조명 교체공사",
         "배전선로 지중화 전기공사", "태양광 발전설비 설치공사", "청사 전기설비 개선공사", "도로 신호등 전기공사"]
COMPANIES = [f"가상전기(주){i:02d}" for i in range(1, 61)]


def lower_rate_for(amt):
    # 예시 값 (실제 하한율은 공고문 확인 필요)
    if amt < 1_000_000_000:
        return 87.745
    if amt < 5_000_000_000:
        return 86.745
    return 85.495


def build(n_bids: int = 600, seed: int = 7) -> str:
    if os.path.exists(SAMPLE_DB_PATH):
        os.remove(SAMPLE_DB_PATH)
    db.use_sample(True)
    db._engines.pop(db.url(), None)  # 파일을 지웠으니 새로 연결
    T = {k: [] for k in db.TABLES}

    def add(table, row):
        T[table].append(row)
    rng = np.random.default_rng(seed)
    random.seed(seed)
    start = datetime.now() - timedelta(days=365)
    # 기관마다 사정율 성향이 약간 다르게 (실제로도 기관별 편차가 있음)
    org_bias = {o: rng.normal(0, 0.25) for o in ORGS}
    for i in range(n_bids):
        org = random.choice(ORGS)
        base = float(round(rng.uniform(80_000_000, 3_900_000_000), -3))
        lr = lower_rate_for(base)
        a_val = float(round(base * rng.uniform(0.02, 0.05), -3)) if rng.random() < 0.7 else 0.0
        upcoming = i >= n_bids - 20  # 마지막 20건은 개찰 전 공고
        if upcoming:
            dt = datetime.now() + timedelta(days=int(rng.integers(1, 14)), hours=int(rng.integers(0, 8)))
        else:
            dt = start + timedelta(days=int(rng.integers(0, 360)), hours=int(rng.integers(9, 17)))
        no = f"R26BK{10000000 + i:08d}"
        key = f"{no}-000"
        add("bids", {
            "bid_key": key, "bid_no": no, "bid_ord": "000",
            "name": f"[샘플] {random.choice(['울산', '남구', '중구', '울주', '양산'])} {random.choice(WORKS)}",
            "org": org, "demand_org": org, "region": random.choice(REGIONS),
            "main_type": "전기공사", "method": "적격심사",
            "notice_dt": (dt - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
            "close_dt": (dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "open_dt": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "qual_dt": (dt - timedelta(days=int(rng.integers(0, 4)), hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "est_price": round(base / 1.1), "lower_rate": lr, "url": "",
        })
        add("base_amounts", {"bid_key": key, "base_amt": base, "range_begin": -2,
                             "range_end": 2, "a_value": a_val})
        if upcoming:
            continue
        # 복수예비가격 15개 (기초금액 ±2%)
        pct = np.sort(rng.uniform(-2, 2, 15) + org_bias[org] * 0.3)
        prices = np.round(base * (1 + pct / 100))
        drawn = rng.choice(15, 4, replace=False)
        plan = float(np.mean(prices[drawn]).round())
        add("plans", {"bid_key": key, "plan_price": plan, "base_amt": base,
                      "open_dt": dt.strftime("%Y-%m-%d %H:%M:%S")})
        for j in range(15):
            add("prelim_prices", {
                "bid_key": key, "sno": j + 1, "price": float(prices[j]),
                "drawn": int(j in drawn), "draw_count": int(rng.integers(0, 20)),
            })
        # 입찰 참가자 투찰
        n = int(rng.integers(80, 900))
        comp = random.sample(COMPANIES, min(len(COMPANIES), max(10, n // 15)))
        s_bid = rng.normal(100 + org_bias[org], 0.9, n)
        amts = np.ceil((base * s_bid / 100 - a_val) * lr / 100 + a_val)
        floor_amt = (plan - a_val) * lr / 100 + a_val
        valid = np.sort(amts[amts >= floor_amt])
        if len(valid) == 0:
            continue
        win = float(valid[0])
        add("openings", {
            "bid_key": key, "open_dt": dt.strftime("%Y-%m-%d %H:%M:%S"), "participants": n,
            "status": "개찰완료", "first_company": comp[0], "first_bizno": "000-00-00000",
            "first_amt": win, "first_rate": round(win / plan * 100, 3),
        })
        if i % 3 == 0:  # 일부 공고만 개찰순위
            for rank, amt in enumerate(valid[: len(comp)], start=1):
                c = comp[rank - 1]
                add("ranks", {"bid_key": key, "rank": rank, "bizno": c, "company": c,
                              "amount": float(amt), "rate": round(amt / plan * 100, 3), "note": ""})
    for table, rows in T.items():
        db.upsert_many(table, rows)
    return SAMPLE_DB_PATH


if __name__ == "__main__":
    print("생성:", build())
