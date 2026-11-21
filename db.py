"""SQLite 데이터베이스 (파일 하나: data/bids.db)."""
import json
import os
import sqlite3

from config import DB_PATH, SAMPLE_DB_PATH

_current = {"path": DB_PATH}


def use_sample(on: bool) -> None:
    """샘플(연습용) DB와 실제 DB 전환."""
    _current["path"] = SAMPLE_DB_PATH if on else DB_PATH


def is_sample() -> bool:
    return _current["path"] == SAMPLE_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    bid_key TEXT PRIMARY KEY,      -- 공고번호-차수
    bid_no TEXT, bid_ord TEXT,
    name TEXT,                     -- 공고명
    org TEXT,                      -- 공고기관
    demand_org TEXT,               -- 수요기관
    region TEXT,                   -- 공사현장지역
    main_type TEXT,                -- 주공종
    method TEXT,                   -- 낙찰방법
    notice_dt TEXT, close_dt TEXT, open_dt TEXT,
    est_price REAL,                -- 추정가격
    lower_rate REAL,               -- 낙찰하한율(%)
    url TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS base_amounts (
    bid_key TEXT PRIMARY KEY,
    base_amt REAL,                 -- 기초금액
    range_begin REAL, range_end REAL,   -- 예가범위(%)
    a_value REAL,                  -- A값(보험료 등 합계)
    raw TEXT
);
CREATE TABLE IF NOT EXISTS prelim_prices (
    bid_key TEXT, sno INTEGER,
    price REAL,                    -- 복수예비가격
    drawn INTEGER,                 -- 추첨여부
    draw_count INTEGER,            -- 추첨횟수
    plan_price REAL,               -- 예정가격
    base_amt REAL,                 -- 기초금액
    open_dt TEXT,
    PRIMARY KEY (bid_key, sno)
);
CREATE TABLE IF NOT EXISTS results (
    bid_key TEXT PRIMARY KEY,
    open_dt TEXT,
    participants INTEGER,          -- 참가업체수
    winner TEXT, winner_bizno TEXT,
    win_amt REAL, win_rate REAL,   -- 낙찰금액, 낙찰률(%)
    raw TEXT
);
CREATE TABLE IF NOT EXISTS ranks (
    bid_key TEXT, rank INTEGER,
    bizno TEXT, company TEXT,
    amount REAL, rate REAL, note TEXT,
    PRIMARY KEY (bid_key, rank, bizno)
);
CREATE TABLE IF NOT EXISTS collect_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT DEFAULT (datetime('now','localtime')),
    kind TEXT, period TEXT, count INTEGER, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_bids_open ON bids(open_dt);
CREATE INDEX IF NOT EXISTS idx_bids_org ON bids(org);
"""


def connect() -> sqlite3.Connection:
    path = _current["path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert(con, table: str, row: dict) -> None:
    cols = list(row.keys())
    sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    con.execute(sql, [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                      for v in row.values()])


def log(con, kind, period, count, message=""):
    con.execute("INSERT INTO collect_log(kind, period, count, message) VALUES (?,?,?,?)",
                (kind, period, count, message))
    con.commit()
