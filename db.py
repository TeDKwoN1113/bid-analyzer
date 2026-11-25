"""데이터베이스.

- DATABASE_URL 이 설정되어 있으면 그 DB(PostgreSQL, 예: Neon)를 사용 → 데이터가 영구 보관됨
- 없으면 내 컴퓨터의 SQLite 파일(data/bids.db)을 사용
"""
import os
from datetime import datetime, timedelta

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
        est_price DOUBLE PRECISION, lower_rate DOUBLE PRECISION, url TEXT, spec_docs TEXT, ntce_kind TEXT,
        qual_dt TEXT, begin_dt TEXT, perf_limit TEXT, pq TEXT, designated TEXT, ability_list TEXT,
        joint_duty TEXT, detail_bid TEXT, joint_rate DOUBLE PRECISION
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
        my_rank INTEGER, checked_at TEXT, created_at TEXT, rec_amount DOUBLE PRECISION, amount_source TEXT
    """, ["bid_key"]),
    # 공고별 진행 관리 (관심·참가신청·투찰 완료·메모) — 공고번호(차수 없이) 기준, 어디서 열어도 같이 보임
    "bid_track": ("""
        bid_no TEXT, fav INTEGER, joined INTEGER, bid_done INTEGER, memo TEXT, updated_at TEXT
    """, ["bid_no"]),
    # 회사 정보 등 사이트·자동수집이 함께 쓰는 설정
    # 공고별 추천 기록 (날마다 마지막 추천) — 추천이 자주 바뀌지 않게, 바뀌면 이력 표시
    "pick_log": ("""
        bid_key TEXT, day TEXT, sajung REAL, amount REAL, prob REAL, at TEXT
    """, ["bid_key", "day"]),
    # 한전(전력그룹사) 전자입찰 공고
    "kepco_bids": ("""
        bid_no TEXT, company TEXT, name TEXT, purchase_type TEXT, item_type TEXT, place TEXT, dept TEXT,
        notice_dt TEXT, attend_close_dt TEXT, begin_dt TEXT, end_dt TEXT, progress TEXT, competition TEXT,
        bid_type TEXT, bid_type_desc TEXT, presumed_price DOUBLE PRECISION, presumed_amount DOUBLE PRECISION,
        base_amt DOUBLE PRECISION, approx_amt DOUBLE PRECISION, contract_amt DOUBLE PRECISION,
        restrict_text TEXT, qualification TEXT, location TEXT, files TEXT, updated_at TEXT
    """, ["bid_no"]),
    # 한전 개찰 결과 (SRM 에서 보고 직접 기록)
    "kepco_results": ("""
        bid_no TEXT, plan_price DOUBLE PRECISION, first_amt DOUBLE PRECISION, lower_rate DOUBLE PRECISION,
        a_value DOUBLE PRECISION, participants INTEGER, our_amount DOUBLE PRECISION, our_rank INTEGER,
        memo TEXT, created_at TEXT
    """, ["bid_no"]),
    # 공고번호로 직접 확인한 참가지역·면허제한 (날짜 조회에서 빠진 경우 대비)
    "limit_checks": ("""
        bid_no TEXT, op TEXT, n INTEGER, checked_at TEXT
    """, ["bid_no", "op"]),
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


WRITES = {"n": 0}  # 저장할 때마다 증가 → 분석 표 메모리 캐시 무효화용
_NO_INVALIDATE = {"kv", "bid_track", "collect_log", "notified", "pick_log", "collect_days"}


def upsert_many(table: str, rows: list[dict]) -> int:
    """여러 행을 한 번에 저장(같은 키가 있으면 덮어씀)."""
    if not rows:
        return 0
    if table not in _NO_INVALIDATE:
        WRITES["n"] += 1
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


READ_BYTES = {"n": 0}  # 이번 실행에서 원격 DB 에서 받은 양(대략) — 무료 전송량 감시용


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    with engine().connect() as c:
        df = pd.read_sql_query(text(sql), c, params=params or {})
    if is_remote() and len(df):
        try:
            READ_BYTES["n"] += int(df.memory_usage(deep=True).sum())
        except Exception:  # noqa: BLE001
            pass
    return df


def query_df_big(sql: str, params: dict | None = None, chunk: int = 20000) -> pd.DataFrame:
    """큰 표를 나눠 받아서 합침 — 한꺼번에 받을 때보다 메모리를 훨씬 덜 씀 (무료 사이트 서버 메모리 한도)."""
    if engine().dialect.name == "sqlite":
        return query_df(sql, params)
    parts = []
    with engine().connect().execution_options(stream_results=True) as c:
        for part in pd.read_sql_query(text(sql), c, params=params or {}, chunksize=chunk):
            parts.append(part)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    del parts
    if is_remote() and len(df):
        try:
            READ_BYTES["n"] += int(df.memory_usage(deep=True).sum())
        except Exception:  # noqa: BLE001
            pass
    return df


def read_mb() -> float:
    return READ_BYTES["n"] / 1048576


def execute(sql: str, params: dict | None = None) -> None:
    WRITES["n"] += 1
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


PRELIM_KEEP_DAYS = 14  # 예비가격 15개 상세는 최근 14일 개찰분만 보관 (무료 500MB 한도 — 예정가격·사정율은 plans 에 계속 남음)


def _nontarget_sql(keywords: list[str]) -> tuple[str, dict]:
    """우리 업종이 아닌 공고(공고명·공종·면허제한 어디에도 키워드 없음, 우리 투찰 기록 없음)의 bid_key 조회 SQL."""
    params, conds = {}, []
    for i, k in enumerate(keywords):
        params[f"k{i}"] = f"%{k}%"
        conds.append(f"b.name LIKE :k{i} OR b.main_type LIKE :k{i}")
    lic_conds = " OR ".join(f"l.license LIKE :k{i}" for i in range(len(keywords)))
    sql = f"""SELECT b.bid_key FROM bids b
                WHERE NOT ({' OR '.join(conds)})
                  AND NOT EXISTS (SELECT 1 FROM licenses l WHERE l.bid_key = b.bid_key AND ({lic_conds}))
                  AND NOT EXISTS (SELECT 1 FROM my_bids m WHERE m.bid_key = b.bid_key)
                  AND EXISTS (SELECT 1 FROM collect_days d
                              WHERE d.op = 'licenses' AND d.day = SUBSTR(b.notice_dt, 1, 10))"""
    return sql, params


def prune_prelim(keywords: list[str]) -> int:
    """우리 업종이 아닌 공고의 '예비가격 15개 상세'를 삭제 (예정가격은 plans 에 남아 분석에 지장 없음)."""
    if not keywords:
        return 0
    sub, params = _nontarget_sql(keywords)
    cutoff = (datetime.now() - timedelta(days=PRELIM_KEEP_DAYS)).strftime("%Y-%m-%d")
    with engine().begin() as c:
        n = c.execute(text(f"DELETE FROM prelim_prices WHERE bid_key IN ({sub})"), params).rowcount or 0
        # 오래된 공고의 15개 상세도 정리 (추첨 번호 통계용, 최근 것만 있으면 충분. 예정가격은 plans 에 남음)
        n += c.execute(text("""DELETE FROM prelim_prices WHERE bid_key IN (
                SELECT p.bid_key FROM plans p WHERE p.open_dt < :cut
                  AND NOT EXISTS (SELECT 1 FROM my_bids m WHERE m.bid_key = p.bid_key))"""),
                       {"cut": cutoff}).rowcount or 0
        return n


def prune_nontarget(keywords: list[str]) -> int:
    """우리 업종이 아닌 공고를 딸린 자료와 함께 삭제 (분석에 쓰지 않음, 무료 저장 한도 때문).

    면허 정보를 받은 날짜의 공고만 판단하므로, 면허제한을 아직 못 받은 공고는 지우지 않음."""
    if not keywords:
        return 0
    sub, params = _nontarget_sql(keywords)
    n = 0
    with engine().begin() as c:
        ids = c.execute(text(sub), params).scalars().all()
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            ph = ",".join(f":b{j}" for j in range(len(chunk)))
            prm = {f"b{j}": k for j, k in enumerate(chunk)}
            # 공고와 딸린 자료를 통째로 삭제 (bids 는 마지막: 위 조건이 bids 를 참조)
            for t in ("licenses", "regions", "base_amounts", "plans", "prelim_prices", "openings",
                      "results", "ranks", "notified", "pick_log", "bids"):
                n += c.execute(text(f"DELETE FROM {t} WHERE bid_key IN ({ph})"), prm).rowcount or 0
    return n


CHILD_TABLES = ("regions", "licenses", "base_amounts", "plans", "prelim_prices", "openings", "results", "ranks")


def _bid_no_expr(col: str = "bid_key") -> str:
    """bid_key('R26BK01843200-000')에서 공고번호('R26BK01843200')만 꺼내는 SQL."""
    if engine().dialect.name == "sqlite":
        return f"CASE WHEN instr({col}, '-') > 0 THEN substr({col}, 1, instr({col}, '-') - 1) ELSE {col} END"
    return f"split_part({col}, '-', 1)"


def known_bid_nos() -> set[str]:
    """bids 표에 있는 공고번호 (다른 업종이라 지운 공고는 없음)."""
    return set(query_df("SELECT DISTINCT bid_no FROM bids")["bid_no"].dropna().astype(str))


def prune_orphans() -> int:
    """공고(bids)는 지웠는데 나중에 다시 들어온 딸린 자료(개찰결과·예비가격 등)를 삭제.

    다른 업종 공고를 지운 뒤에도 개찰결과·예비가격은 날짜별로 계속 들어오므로, 주인 없는 자료가 쌓여
    저장 공간을 차지함. 국방(D2B…)·한전(KEPCO-…)·우리 투찰 기록은 건드리지 않음."""
    n = 0
    with engine().begin() as c:
        for t in CHILD_TABLES:
            n += c.execute(text(f"""DELETE FROM {t} WHERE bid_key NOT LIKE 'KEPCO-%' AND bid_key NOT LIKE 'D2B%'
                    AND NOT EXISTS (SELECT 1 FROM bids b WHERE b.bid_no = {_bid_no_expr(t + '.bid_key')})
                    AND NOT EXISTS (SELECT 1 FROM my_bids m WHERE m.bid_key = {t}.bid_key)""")).rowcount or 0
    return n


def vacuum() -> None:
    """지운 자리를 새 데이터가 다시 쓰도록 정리 (PostgreSQL)."""
    if engine().dialect.name != "postgresql":
        return
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM (ANALYZE)"))


def compact(limit_mb: float) -> list[str]:
    """테이블을 새로 써서 실제 용량을 줄임 (VACUUM FULL). 작은 테이블부터, 한도를 넘지 않을 때만.

    VACUUM FULL 은 잠시 테이블 사본만큼 공간이 더 필요하므로 '현재 용량 + 테이블 크기 < 한도'일 때만 실행."""
    if engine().dialect.name != "postgresql":
        return []
    sizes = query_df("""SELECT s.relname AS t, pg_total_relation_size(s.relid) / 1048576.0 AS mb,
                               pg_relation_size(s.relid) / 1048576.0 AS heap_mb, c.reltuples AS n
                        FROM pg_catalog.pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid""")
    est = {}
    for t, mb, heap, n in zip(sizes["t"], sizes["mb"], sizes["heap_mb"], sizes["n"]):
        if t not in TABLES:
            continue
        # 새로 쓴 뒤 크기 추정 = 행 수 × 실제 행 크기 (지운 자리는 빠짐) + 색인 몫. 모르면 지금 크기.
        try:
            w = float(query_df(f"SELECT avg(pg_column_size(x.*)) AS w FROM (SELECT * FROM {t} LIMIT 2000) x")["w"].iloc[0] or 0)
        except Exception:  # noqa: BLE001
            w = 0
        if n and n > 0 and w > 0 and heap > 0:
            live_heap = n * (w + 28) / 1048576
            # 색인은 새로 만들면 행 수에 비례 (지운 자리로 부푼 지금 색인 크기 비율을 쓰면 과대 추정)
            est[t] = min(mb, live_heap * 1.2 + n * 60 / 1048576 + 1)
        else:
            est[t] = mb
    done = []
    cur_sizes = dict(zip(sizes["t"], sizes["mb"]))
    # 필요한 여유(새로 쓴 크기)가 작은 표부터 → 먼저 줄인 만큼 다음 표의 여유가 생김
    for t in sorted(est, key=est.get):
        mb = est[t]
        if cur_sizes.get(t, 0) - mb < 0.5:
            continue  # 줄어들 게 거의 없음
        cur = size_mb() or 0
        if cur + mb >= limit_mb * 0.99:
            # 여유가 모자라면 먼저 색인만 새로 만들어(필요 공간 작음) 부푼 색인 공간을 돌려받고 다시 확인
            try:
                with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                    c.execute(text(f"REINDEX TABLE {t}"))
                cur = size_mb() or 0
            except Exception as e:  # noqa: BLE001
                done.append(f"{t} 색인 재구성 실패({str(e)[:40]})")
        if cur + mb >= limit_mb * 0.99:
            done.append(f"{t} 건너뜀(여유 부족, 현재 {cur:.0f}MB + 필요 {mb:.0f}MB)")
            continue
        try:
            with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.execute(text(f"VACUUM (FULL, ANALYZE) {t}"))
            done.append(t)
        except Exception as e:  # noqa: BLE001
            done.append(f"{t} 실패({str(e)[:60]})")
    return done


def _record_month_egress() -> None:
    """자동 수집(GitHub Actions) 실행이 끝날 때 이번 달 누적 내려받은 양을 기록 (무료 전송량 월 5GB 감시).
    사이트에서 보는 양은 포함되지 않으므로 실제 사용량은 이보다 조금 많음."""
    mb = read_mb()
    if not os.environ.get("GITHUB_ACTIONS") or mb < 0.5:
        return
    try:
        if not is_remote():
            return
        k = "egress_mb_" + datetime.now().strftime("%Y%m")
        total = float(get_kv(k) or 0) + mb
        set_kv(k, f"{total:.1f}")
        print(f"이번 달 자동 수집 DB 내려받은 양 누적(대략): {total:,.0f}MB / 무료 한도 5,000MB"
              + ("  ⚠️ 한도의 70% 넘음" if total > 3500 else ""))
    except Exception:  # noqa: BLE001
        pass


import atexit  # noqa: E402
atexit.register(_record_month_egress)
