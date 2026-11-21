"""데이터베이스.

- DATABASE_URL 이 설정되어 있으면 그 DB(PostgreSQL, 예: Neon)를 사용 → 데이터가 영구 보관됨
- 없으면 내 컴퓨터의 SQLite 파일(data/bids.db)을 사용
"""
import os
from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine, text

from config import DB_PATH, SAMPLE_DB_PATH, load_settings

_state = {"sample": False}
_engines: dict = {}

# 테이블: (컬럼 정의, 기본키)
TABLES = {
    "bids": ("""
        bid_key TEXT, bid_no TEXT, bid_ord TEXT,
        name TEXT, org TEXT, demand_org TEXT, region TEXT, main_type TEXT,
        method TEXT, contract_method TEXT,
        notice_dt TEXT, close_dt TEXT, open_dt TEXT,
        est_price DOUBLE PRECISION, lower_rate DOUBLE PRECISION, url TEXT
    """, ["bid_key"]),
    # 참가가능지역 (없으면 지역제한 없음)
    "regions": ("""
        bid_key TEXT, region TEXT
    """, ["bid_key", "region"]),
    # 면허(업종) 제한
    "licenses": ("""
        bid_key TEXT, license TEXT
    """, ["bid_key", "license"]),
    "base_amounts": ("""
        bid_key TEXT, base_amt DOUBLE PRECISION,
        range_begin DOUBLE PRECISION, range_end DOUBLE PRECISION, a_value DOUBLE PRECISION,
        a_apply INTEGER, pure_cost DOUBLE PRECISION, a_detail TEXT
    """, ["bid_key"]),
    # 공고별 예정가격 (예비가격 상세에서 추출)
    "plans": ("""
        bid_key TEXT, plan_price DOUBLE PRECISION, base_amt DOUBLE PRECISION, open_dt TEXT
    """, ["bid_key"]),
    # 복수예비가격 15개
    "prelim_prices": ("""
        bid_key TEXT, sno INTEGER, price DOUBLE PRECISION, drawn INTEGER, draw_count INTEGER
    """, ["bid_key", "sno"]),
    # 개찰결과 (개찰 직후 공개: 참가업체수, 1순위)
    "openings": ("""
        bid_key TEXT, open_dt TEXT, participants INTEGER, status TEXT,
        first_company TEXT, first_bizno TEXT, first_amt DOUBLE PRECISION, first_rate DOUBLE PRECISION
    """, ["bid_key"]),
    # 최종 낙찰자 (적격심사 후 확정)
    "results": ("""
        bid_key TEXT, open_dt TEXT, participants INTEGER, winner TEXT, winner_bizno TEXT,
        win_amt DOUBLE PRECISION, win_rate DOUBLE PRECISION
    """, ["bid_key"]),
    # 업체별 개찰순위 (공고별로 받음)
    "ranks": ("""
        bid_key TEXT, rank INTEGER, bizno TEXT, company TEXT,
        amount DOUBLE PRECISION, rate DOUBLE PRECISION, note TEXT
    """, ["bid_key", "rank", "bizno"]),
    # 날짜별 수집 완료 기록 (자동 수집/과거 데이터 채우기용)
    "collect_days": ("""
        op TEXT, day TEXT, rows INTEGER, done_at TEXT
    """, ["op", "day"]),
    # 알림 보낸 기록 (같은 알림 중복 방지)
    "notified": ("""
        bid_key TEXT, kind TEXT, sent_at TEXT
    """, ["bid_key", "kind"]),
    # 우리 회사 투찰 기록
    "my_bids": ("""
        bid_key TEXT, amount DOUBLE PRECISION, sajung DOUBLE PRECISION, memo TEXT,
        my_rank INTEGER, checked_at TEXT, created_at TEXT
    """, ["bid_key"]),
    # 회사 정보 등 사이트·자동수집이 함께 쓰는 설정
    "kv": ("""
        k TEXT, v TEXT
    """, ["k"]),
    "collect_log": ("""
        run_at TEXT, kind TEXT, period TEXT, count INTEGER, message TEXT
    """, None),
}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bids_open ON bids(open_dt)",
    "CREATE INDEX IF NOT EXISTS idx_bids_org ON bids(org)",
    "CREATE INDEX IF NOT EXISTS idx_log_run ON collect_log(run_at)",
]


def use_sample(on: bool) -> None:
    """샘플(연습용) DB와 실제 DB 전환."""
    _state["sample"] = on


def is_sample() -> bool:
    return _state["sample"]


def url() -> str:
    if _state["sample"]:
        return f"sqlite:///{SAMPLE_DB_PATH}"
    u = load_settings().get("database_url") or ""
    if u:
        # Neon/Supabase 가 주는 postgres:// 형식도 허용
        for prefix in ("postgres://", "postgresql://"):
            if u.startswith(prefix):
                u = "postgresql+psycopg://" + u[len(prefix):]
        return u
    return f"sqlite:///{DB_PATH}"


def is_remote() -> bool:
    return not url().startswith("sqlite")


def engine():
    u = url()
    if u not in _engines:
        if u.startswith("sqlite"):
            os.makedirs(os.path.dirname(u.replace("sqlite:///", "")), exist_ok=True)
            eng = create_engine(u)
        else:
            eng = create_engine(u, pool_pre_ping=True, pool_recycle=300)
        _init(eng)
        _engines[u] = eng
    return _engines[u]


def _columns(cols_sql: str) -> list[tuple[str, str]]:
    out = []
    for part in cols_sql.replace("\n", " ").split(","):
        part = part.strip()
        if part:
            name, typ = part.split(None, 1)
            out.append((name, typ))
    return out


def _init(eng) -> None:
    with eng.begin() as c:
        for name, (cols, pk) in TABLES.items():
            pk_sql = f", PRIMARY KEY ({', '.join(pk)})" if pk else ""
            c.execute(text(f"CREATE TABLE IF NOT EXISTS {name} ({cols}{pk_sql})"))
        for ix in INDEXES:
            c.execute(text(ix))
    # 새 버전에서 추가된 컬럼이 기존 테이블에 없으면 추가 (자동 업그레이드)
    from sqlalchemy import inspect
    insp = inspect(eng)
    added = []
    with eng.begin() as c:
        for name, (cols, _) in TABLES.items():
            have = {x["name"] for x in insp.get_columns(name)}
            for col, typ in _columns(cols):
                if col not in have:
                    c.execute(text(f"ALTER TABLE {name} ADD COLUMN {col} {typ}"))
                    added.append((name, col))
        # 기초금액에 A값 적용여부·순공사원가가 새로 생겼으면 과거 기초금액을 다시 받도록 표시 삭제
        if ("base_amounts", "a_apply") in added:
            c.execute(text("DELETE FROM collect_days WHERE op = 'base_amount'"))


def upsert_many(table: str, rows: list[dict]) -> int:
    """여러 행을 한 번에 저장(같은 키가 있으면 덮어씀)."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    pk = TABLES[table][1]
    ph = ", ".join(f":{c}" for c in cols)
    if not pk:
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"
    elif engine().dialect.name == "sqlite":
        sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({ph})"
    else:
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
               f"ON CONFLICT ({', '.join(pk)}) DO " + (f"UPDATE SET {upd}" if upd else "NOTHING"))
    # 같은 키가 한 묶음에 두 번 있으면 PostgreSQL 이 거부하므로 마지막 값만 남김
    if pk:
        dedup = {tuple(r[k] for k in pk): r for r in rows}
        rows = list(dedup.values())
    with engine().begin() as c:
        for i in range(0, len(rows), 1000):
            c.execute(text(sql), rows[i:i + 1000])
    return len(rows)


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    with engine().connect() as c:
        return pd.read_sql_query(text(sql), c, params=params or {})


def execute(sql: str, params: dict | None = None) -> None:
    with engine().begin() as c:
        c.execute(text(sql), params or {})


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(kind, period, count, message=""):
    upsert_many("collect_log", [{"run_at": now(), "kind": kind, "period": period,
                                 "count": count, "message": message[:1000]}])


def done_days(op: str) -> set[str]:
    df = query_df("SELECT day FROM collect_days WHERE op = :op", {"op": op})
    return set(df["day"])


def mark_day(op: str, day: str, rows: int) -> None:
    upsert_many("collect_days", [{"op": op, "day": day, "rows": rows, "done_at": now()}])


_kv_cache: dict = {}


def get_kv(k: str, default=None):
    """설정값 읽기 (원격 DB 왕복을 줄이려고 30초 캐시)."""
    import time
    key = (url(), k)
    hit = _kv_cache.get(key)
    if hit and time.time() - hit[0] < 30:
        v = hit[1]
    else:
        df = query_df("SELECT v FROM kv WHERE k = :k", {"k": k})
        v = df["v"].iloc[0] if len(df) else None
        _kv_cache[key] = (time.time(), v)
    return default if v is None else v


def set_kv(k: str, v: str) -> None:
    upsert_many("kv", [{"k": k, "v": v}])
    _kv_cache.pop((url(), k), None)


def company() -> dict:
    """회사 정보: DB에 저장된 값 우선, 없으면 설정 기본값."""
    s = load_settings()
    lic = get_kv("company_licenses")
    return {
        "name": get_kv("company_name", ""),
        "bizno": (get_kv("company_bizno", "") or "").replace("-", ""),
        "region": get_kv("company_region", s["company_region"]),
        "licenses": [x.strip() for x in lic.split(",") if x.strip()] if lic else s["company_licenses"],
    }


# ---------------- 저장 공간 관리 ----------------

def size_mb() -> float | None:
    try:
        if engine().dialect.name == "sqlite":
            return os.path.getsize(url().replace("sqlite:///", "")) / 1048576
        return float(query_df("SELECT pg_database_size(current_database()) / 1048576.0 AS mb")["mb"].iloc[0])
    except Exception:  # noqa: BLE001
        return None


def table_sizes() -> pd.DataFrame:
    if engine().dialect.name == "sqlite":
        rows = []
        for t in TABLES:
            n = int(query_df(f"SELECT COUNT(*) AS n FROM {t}")["n"].iloc[0])
            rows.append({"테이블": t, "행 수": n, "MB": None})
        return pd.DataFrame(rows)
    return query_df(
        """SELECT relname AS "테이블", n_live_tup AS "행 수",
                  ROUND(pg_total_relation_size(relid) / 1048576.0, 1) AS "MB"
           FROM pg_catalog.pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC""")


def prune_prelim(keywords: list[str]) -> int:
    """우리 업종이 아닌 공고의 '예비가격 15개 상세'를 삭제 (예정가격은 plans 에 남아 분석에 지장 없음)."""
    if not keywords:
        return 0
    params, conds = {}, []
    for i, k in enumerate(keywords):
        params[f"k{i}"] = f"%{k}%"
        conds.append(f"b.name LIKE :k{i} OR b.main_type LIKE :k{i}")
    lic_conds = " OR ".join(f"l.license LIKE :k{i}" for i in range(len(keywords)))
    sql = f"""DELETE FROM prelim_prices WHERE bid_key IN (
                SELECT b.bid_key FROM bids b
                WHERE NOT ({' OR '.join(conds)})
                  AND NOT EXISTS (SELECT 1 FROM licenses l WHERE l.bid_key = b.bid_key AND ({lic_conds}))
                  AND NOT EXISTS (SELECT 1 FROM my_bids m WHERE m.bid_key = b.bid_key))"""
    with engine().begin() as c:
        res = c.execute(text(sql), params)
        return res.rowcount or 0
