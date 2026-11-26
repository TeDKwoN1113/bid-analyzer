"""DB(Supabase) 시계로 자동 수집을 정시에 시작시키기.

GitHub 예약 실행은 자주 빠짐(하루 19번 중 1~3번만 도는 날이 많음). 그래서 Supabase 의 pg_cron(시계) + pg_net(인터넷 요청)으로
정해진 시간마다 GitHub 에 "수집 시작(mode=scheduled)" 요청을 보냄. 실제로 전체/빠른 수집을 할지, 바로 끝낼지는
collector.py --scheduled 가 정하므로 GitHub 예약과 겹쳐도 문제없음.

실행: Actions → daily-collect.yml → mode=scheduler (GitHub Secrets 에 DISPATCH_TOKEN 필요)
토큰은 DB 안의 비밀 금고(vault)에만 저장되고 화면·기록에는 나오지 않음.
"""
import os
import sys
import time

import requests
from sqlalchemy import text

import db
from config import load_settings

REPO = os.environ.get("GITHUB_REPOSITORY", "TeDKwoN1113/bid-analyzer")
WORKFLOW = "daily-collect.yml"
SECRET_NAME = "gh_dispatch_token"
PREFIX = "bid_collect_"
# (이름, cron — UTC 기준). 한국시간 = UTC + 9
JOBS = [
    ("dawn", "41 19,20,21 * * *"),      # 매일 04:41 05:41 06:41 — 전체 수집 (이미 했으면 바로 끝남)
    ("day", "23 2,6 * * 1-5"),          # 평일 11:23 15:23 — 빠른 수집
]
# 2026-11-26: Supabase 무료 전송량(5GB) 초과 → 낮 수집을 2번으로 줄임.
# 사이트 깨우기는 비밀번호 화면에서 멈춰 분석 자료를 받지 않으므로(전송량 거의 없음) 하루 3번으로 유지
WAKE_ENABLED = True
# 사이트 깨우기(한전 공고는 사이트 서버에서만 받음) — 한국시간 06:37 12:37 18:37
WAKE = ("wake_site", "37 21,3,9 * * *", "wake-site.yml")


def _check_token(tok: str) -> None:
    r = requests.get(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}",
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"DISPATCH_TOKEN 으로 저장소에 접근할 수 없습니다 (HTTP {r.status_code}). 토큰 권한(Actions: Read and write)과 "
                 "대상 저장소를 확인하세요.")


def main() -> None:
    load_settings()
    tok = (os.environ.get("DISPATCH_TOKEN") or "").strip()
    if not tok:
        sys.exit("GitHub Secrets 에 DISPATCH_TOKEN 이 없습니다. 먼저 토큰을 만들어 넣어 주세요.")
    if not db.is_remote():
        sys.exit("원격 DB(DATABASE_URL)가 아닙니다.")
    _check_token(tok)
    print("토큰 확인: 저장소 접근 가능")
    eng = db.engine()
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_net"))
    print("DB 시계(pg_cron)·인터넷 요청(pg_net) 켬")
    # 토큰을 DB 비밀 금고에 보관 (이미 있으면 바꿈)
    with eng.begin() as c:
        c.execute(text("DELETE FROM vault.secrets WHERE name = :n"), {"n": SECRET_NAME})
        c.execute(text("SELECT vault.create_secret(:t, :n, 'GitHub 자동 수집 시작용')"), {"t": tok, "n": SECRET_NAME})
    print("토큰을 DB 비밀 금고에 저장 (화면에 나오지 않음)")
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    call = (
        "SELECT net.http_post("
        f"url := '{url}', "
        "headers := jsonb_build_object("
        f"'Authorization', 'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = '{SECRET_NAME}'), "
        "'Accept', 'application/vnd.github+json', 'User-Agent', 'bid-analyzer-db-cron', "
        "'X-GitHub-Api-Version', '2022-11-28'), "
        "body := '{\"ref\":\"main\",\"inputs\":{\"mode\":\"scheduled\"}}'::jsonb)"
    )
    with eng.begin() as c:
        old = c.execute(text("SELECT jobname FROM cron.job WHERE jobname LIKE :p"), {"p": PREFIX + "%"}).fetchall()
        for (name,) in old:
            c.execute(text("SELECT cron.unschedule(:n)"), {"n": name})
        for name, cron in JOBS:
            c.execute(text("SELECT cron.schedule(:n, :c, :cmd)"), {"n": PREFIX + name, "c": cron, "cmd": call})
        if WAKE_ENABLED:
            wake_call = call.replace(WORKFLOW, WAKE[2]).replace(',\"inputs\":{\"mode\":\"scheduled\"}', "")
            c.execute(text("SELECT cron.schedule(:n, :c, :cmd)"), {"n": PREFIX + WAKE[0], "c": WAKE[1], "cmd": wake_call})
    print(f"DB 시계에 수집 예약 {len(JOBS)}개" + (" + 사이트 깨우기 1개" if WAKE_ENABLED else " (사이트 깨우기 꺼짐)") + " 등록 (GitHub 예약이 빠져도 정시에 시작)")
    # 지금 한 번 보내서 실제로 되는지 확인
    with eng.begin() as c:
        rid = c.execute(text(call)).scalar()
    status = None
    for _ in range(20):
        time.sleep(3)
        with eng.begin() as c:
            row = c.execute(text("SELECT status_code, error_msg FROM net._http_response WHERE id = :i"),
                            {"i": rid}).fetchone()
        if row:
            status = row
            break
    if not status:
        sys.exit("시험 요청의 응답을 받지 못했습니다. 잠시 후 다시 실행해 보세요.")
    if status[0] != 204:
        sys.exit(f"시험 요청 실패: HTTP {status[0]} {str(status[1] or '')[:200]}")
    print("시험 요청 성공: GitHub 가 수집 시작 요청을 받음 (HTTP 204)")
    with eng.begin() as c:
        rows = c.execute(text("SELECT jobname, schedule, active FROM cron.job WHERE jobname LIKE :p ORDER BY jobname"),
                         {"p": PREFIX + "%"}).fetchall()
    for r in rows:
        print(f"  · {r[0]}  {r[1]} (UTC)  {'켜짐' if r[2] else '꺼짐'}")


if __name__ == "__main__":
    main()


def health() -> str | None:
    """DB 시계 요청이 최근 하루 동안 실패했으면 경고 문장 (토큰 만료·권한 문제 등). 정상이거나 설정 전이면 None."""
    try:
        with db.engine().begin() as c:
            jobs = c.execute(text("SELECT count(*) FROM cron.job WHERE jobname LIKE :p"), {"p": PREFIX + "%"}).scalar()
            if not jobs:
                return None
            bad = c.execute(text("SELECT status_code, count(*) FROM net._http_response "
                                 "WHERE created > now() - interval '26 hours' AND coalesce(status_code, 0) <> 204 "
                                 "GROUP BY 1")).fetchall()
    except Exception:  # noqa: BLE001  pg_cron 이 없는 DB(샘플 등)
        return None
    if not bad:
        return None
    detail = ", ".join(f"HTTP {s or '응답없음'} {n}건" for s, n in bad)
    hint = " — 토큰(DISPATCH_TOKEN)이 만료됐거나 권한이 바뀌었을 수 있음" if any(s in (401, 403, 404) for s, _ in bad) else ""
    return f"DB 시계의 자동 수집 시작 요청이 실패했습니다 ({detail}){hint}. 그동안은 GitHub 예약 실행만 돕니다."
