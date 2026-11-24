"""알림: 새 공고 / 기초금액 공개(추천 투찰금액) / 개찰 결과 → 텔레그램·이메일.

    python notify.py            # 알림 보내기 (보낸 건은 다시 안 보냄)
    python notify.py --dry-run  # 보내지 않고 내용만 출력
    python notify.py --test     # 테스트 메시지
"""
import argparse
import html
import re
import smtplib
from email.mime.text import MIMEText

import pandas as pd
import requests

import analysis
import db
from config import load_settings

TG_LIMIT = 3800  # 텔레그램 메시지 1개 최대 길이(여유 포함)


# ---------------- 보내기 ----------------

def channels(s=None) -> list[str]:
    s = s or load_settings()
    out = []
    if s.get("telegram_bot_token") and s.get("telegram_chat_id"):
        out.append("텔레그램")
    if s.get("email_user") and s.get("email_password") and s.get("email_to"):
        out.append("이메일")
    return out


def send_telegram(text: str, s=None) -> None:
    s = s or load_settings()
    # 공고 단위("• "로 시작)로 나눠서 메시지 길이 제한을 넘지 않게
    units = re.split(r"\n(?=• |\n)", text)
    chunks, cur = [], ""
    for u in units:
        if len(cur) + len(u) + 1 > TG_LIMIT and cur:
            chunks.append(cur)
            cur = ""
        cur += u + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{s['telegram_bot_token']}/sendMessage",
                          json={"chat_id": s["telegram_chat_id"], "text": c.strip(), "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text[:300]}")


def send_email(subject: str, text: str, s=None) -> None:
    s = s or load_settings()
    body = text.replace("\n", "<br>")
    msg = MIMEText(f"<div style='font-family:sans-serif;font-size:14px'>{body}</div>", "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = s["email_user"]
    to = [t.strip() for t in s["email_to"].split(",") if t.strip()]
    msg["To"] = ", ".join(to)
    with smtplib.SMTP_SSL(s.get("email_smtp") or "smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(s["email_user"], s["email_password"])
        smtp.sendmail(s["email_user"], to, msg.as_string())


def send(subject: str, text: str) -> list[str]:
    s = load_settings()
    sent = []
    if "텔레그램" in channels(s):
        send_telegram(f"<b>{html.escape(subject)}</b>\n\n{text}", s)
        sent.append("텔레그램")
    if "이메일" in channels(s):
        send_email(subject, text, s)
        sent.append("이메일")
    return sent


def telegram_chats(token: str) -> list[dict]:
    """봇에게 말을 건 대화방 목록 (chat_id 찾기용)."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    r.raise_for_status()
    chats = {}
    for u in r.json().get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat")
        if c:
            name = c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")]))
            chats[c["id"]] = {"chat_id": c["id"], "이름": name, "종류": c.get("type")}
    return list(chats.values())


# ---------------- 내용 만들기 ----------------

def _e(x) -> str:
    return html.escape("" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x))


def _won(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:,.0f}원"


def _dt(x) -> str:
    t = pd.to_datetime(x, errors="coerce", format="mixed")
    return "-" if pd.isna(t) else t.strftime("%m/%d(%a) %H:%M").replace(
        "Mon", "월").replace("Tue", "화").replace("Wed", "수").replace("Thu", "목").replace(
        "Fri", "금").replace("Sat", "토").replace("Sun", "일")


def _title(r) -> str:
    name = _e(r["name"])
    if isinstance(r.get("url"), str) and r["url"].startswith("http"):
        return f'<a href="{html.escape(r["url"])}">{name}</a>'
    return f"<b>{name}</b>"


SCOPES = {"local": "우리 지역(울산) 공고만", "all": "참가 가능한 전국 공고 전체"}


def scope() -> str:
    """알림 범위 (기본: 우리 지역만). 사이트 설정에서 바꿈."""
    v = db.get_kv("notify_scope") or "local"
    return v if v in SCOPES else "local"


DETAIL_MAX = 15  # 새 공고·개찰 결과는 한 번에 이만큼만 자세히 (나머지는 건수만)


def _focus_line(df, r) -> str:
    """경쟁이 적을 것으로 예상되는 공고 표시 (과거 비슷한 공고의 참가업체 수)."""
    e = analysis.expected_competition(df, r, analysis.bid_keywords(r))
    if e is None:
        return ""
    star = "⭐ 집중 — " if e <= analysis.FOCUS_MAX_PARTICIPANTS else ""
    return f"\n  {star}예상 참가 약 {e:.0f}곳"


def _more(lines: list[str], n: int) -> None:
    if n > DETAIL_MAX:
        lines.append(f"… 외 {n - DETAIL_MAX}건은 사이트에서 확인")


def build(df: pd.DataFrame | None = None) -> tuple[str, list[tuple[str, str]]]:
    """보낼 메시지 본문과 (공고키, 종류) 목록."""
    s = load_settings()
    kw = s["notify_keywords"] or analysis.company_keywords()
    rg = s["notify_regions"] or s["default_regions"]
    mx = s["notify_max_amount"] or s["max_amount"]
    df = analysis.load_dataset() if df is None else df
    if df.empty:
        return "", []
    t = analysis.apply_filters(df, keywords=kw, regions=rg, max_amt=mx, eligible_only=True)
    if scope() == "local":
        t = t[analysis.local_mask(t)]
    phist = analysis.pick_history()
    sent = db.query_df("SELECT bid_key, kind FROM notified")
    done = set(zip(sent["bid_key"], sent["kind"]))
    now = pd.Timestamp.now()
    notice = pd.to_datetime(t["notice_dt"], errors="coerce", format="mixed")
    keys, parts = [], []

    # 0) 오늘 마감인데 아직 투찰 기록이 없는 공고 → 투찰금액과 함께 다시 알림
    close = pd.to_datetime(t["close_dt"], errors="coerce", format="mixed")
    mine = set(db.query_df("SELECT bid_key FROM my_bids")["bid_key"].str.rsplit("-", n=1).str[0])
    dl = t[(close >= now) & (close <= now.normalize() + pd.Timedelta(days=1)) & t["base_amt_notice"].notna()]
    dl = dl[~dl["bid_no"].isin(mine)]
    dl = dl[~dl["bid_key"].map(lambda k: (k, "deadline") in done).astype(bool)]
    if len(dl):
        lines = [f"⏰ <b>오늘 투찰 마감 {len(dl)}건</b> (아직 기록 없음)"]
        for _, r in dl.sort_values("close_dt").iterrows():
            pk = analysis.pick_bid(df, r, analysis.bid_keywords(r), prev=analysis.prev_pick(phist, r["bid_key"]))
            amt = f"💰 <b>{pk['투찰금액']:,}원</b> (사정율 {pk['사정율']:.2f}%)" if pk else "투찰금액 계산 불가"
            ct = pd.to_datetime(r["close_dt"], errors="coerce", format="mixed")
            lines.append(f"• {_title(r)}\n  마감 {ct.strftime('%H:%M') if pd.notna(ct) else '-'} | {amt}")
            keys.append((r["bid_key"], "deadline"))
        parts.append("\n".join(lines))

    # 0-1) 입찰참가등록 마감 임박 (국방·한전 — 등록 못 하면 투찰 불가). 전날·당일 두 번 알림
    def _when(ts):
        d = (ts.normalize() - now.normalize()).days
        return ("qual0", "오늘") if d <= 0 else ("qual1", "내일")
    reg = []
    qd = analysis.reg_deadline(t)
    qq = t.assign(_q=qd)
    qq = qq[(qq["_q"] >= now) & (qq["_q"] < now.normalize() + pd.Timedelta(days=2)) & ~qq["bid_no"].isin(mine)]
    for _, r in qq.sort_values("_q").iterrows():
        kind, label = _when(r["_q"])
        if (r["bid_key"], kind) not in done:
            reg.append((r["_q"], f"• [국방] {_title(r)}\n  ⛔ 참가등록 마감 <b>{label} {r['_q'].strftime('%H:%M')}</b> | "
                                 f"투찰마감 {_dt(r['close_dt'])} | 국방전자조달(d2b.go.kr)", (r["bid_key"], kind)))
    try:
        import kepco
        kb = kepco.load(recent_days=10)
        if not kb.empty:
            kb = kepco.filter_bids(kb, region=db.company()["region"], local_only=True)
            kb = kb[(kb["attend_close_dt"] >= now) & (kb["attend_close_dt"] < now.normalize() + pd.Timedelta(days=2))]
            for _, r in kb.sort_values("attend_close_dt").iterrows():
                kind, label = _when(r["attend_close_dt"])
                key = "KEPCO-" + r["bid_no"]
                if (key, kind) not in done:
                    reg.append((r["attend_close_dt"], f"• [한전] {_e(r['name'])}\n  ⛔ 입찰신청 마감 <b>{label} "
                                f"{r['attend_close_dt'].strftime('%H:%M')}</b> | 입찰마감 {_dt(r['end_dt'])} | 한전 SRM",
                                (key, kind)))
    except Exception as e:  # noqa: BLE001
        print("한전 참가신청 알림 생략:", e)
    if reg:
        reg.sort(key=lambda x: x[0])
        lines = [f"⛔ <b>입찰참가등록 마감 임박 {len(reg)}건</b> (등록하지 않으면 투찰할 수 없어요)"]
        lines += [x[1] for x in reg]
        keys += [x[2] for x in reg]
        parts.insert(0, "\n".join(lines))

    # 1) 새 공고 (최근 3일 내 공고, 아직 개찰 전)
    new = t[(t["open_date"] >= now) & (notice >= now - pd.Timedelta(days=3))]
    new = new[~new["bid_key"].map(lambda k: (k, "new") in done).astype(bool)].sort_values("open_date")
    if len(new):
        lines = [f"🆕 <b>새 공고 {len(new)}건</b>"]
        keys += [(k, "new") for k in new["bid_key"].iloc[DETAIL_MAX:]]
        for _, r in new.head(DETAIL_MAX).iterrows():
            amt = r["base_amt"] if pd.notna(r["base_amt"]) else r["est_price"]
            limit = r["allowed_regions"] or ("제한없음" if r["allowed_regions_known"] else _e(r["region"]))
            lines.append(f"• {_title(r)}\n  {_e(r['org'])} | 참가지역 {_e(limit)}\n"
                         f"  {'기초' if pd.notna(r['base_amt']) else '추정'} {_won(amt)} | "
                         f"개찰 {_dt(r['open_dt'])} | {_e(r['contract_method'] or r['method'])}"
                         + (f"\n  ⛔ 참가등록 마감 {_dt(r['qual_dt'])} (국방전자조달)" if analysis.is_d2b(r["bid_key"])
                            and pd.notna(pd.to_datetime(r.get('qual_dt'), errors='coerce', format='mixed')) else "")
                         + ("\n  💲 최저가 내역서 입찰 (내역서·도면 검토 후 견적)" if bool(analysis.lowest_price(pd.DataFrame([r])).iloc[0])
                            else _focus_line(df, r)))
            keys.append((r["bid_key"], "new"))
        _more(lines, len(new))
        parts.append("\n".join(lines))

    # 2) 기초금액 공개 → 추천 투찰금액
    base = t[(t["open_date"] >= now) & t["base_amt_notice"].notna()]
    base = base[~base["bid_key"].map(lambda k: (k, "base") in done).astype(bool)].sort_values("open_date")
    if len(base):
        lines = [f"💰 <b>기초금액 공개 {len(base)}건</b>"]
        for _, r in base.iterrows():
            lr = "-" if pd.isna(r["lower_rate"]) else f"{r['lower_rate']:.3f}%"
            av = f" | A값 {_won(r['a_value'])}" if r["a_value"] else ""
            line = (f"• {_title(r)}\n  {_e(r['org'])} | 개찰 {_dt(r['open_dt'])}\n"
                    f"  기초금액 {_won(r['base_amt'])} | 하한율 {lr}{av}")
            is_lp = bool(analysis.lowest_price(pd.DataFrame([r])).iloc[0])
            pk = None if is_lp else analysis.pick_bid(df, r, analysis.bid_keywords(r),
                                                      prev=analysis.prev_pick(phist, r["bid_key"]))
            if is_lp:
                line += f"\n  💲 {_e(analysis.LOWEST_NOTE)}"
            elif pk:
                badge = {"높음": "🟢", "보통": "🟡", "낮음": "🔴"}[pk["신뢰도"]]
                qok, qmsg = pk.get("적격심사") or (None, "")
                if qok is False:
                    line += f"\n  ⚠️ {_e(qmsg)}"
                line += (f"\n  💰 투찰금액 <b>{pk['투찰금액']:,}원</b>"
                         f"\n     사정율 {pk['사정율']:.2f}% · 신뢰도 {badge}{pk['신뢰도']}"
                         + "".join(f"\n     · {_e(x)}" for x in analysis.evidence_lines(pk)[:4]))
            else:
                line += "\n  💰 하한율 정보가 없어 금액 계산 불가 (공고문 확인)"
            lines.append(line)
            keys.append((r["bid_key"], "base"))
        parts.append("\n".join(lines))

    # 3) 개찰 결과 (최근 3일)
    res = t[(t["open_date"] < now) & (t["open_date"] >= now - pd.Timedelta(days=3))
            & (t["first_company"].notna() | t["plan_price"].notna())]
    res = res[~res["bid_key"].map(lambda k: (k, "result") in done).astype(bool)].sort_values("open_date")
    if len(res):
        lines = [f"📊 <b>개찰 결과 {len(res)}건</b>"]
        res = res.iloc[::-1]  # 최근 개찰부터
        keys += [(k, "result") for k in res["bid_key"].iloc[DETAIL_MAX:]]
        for _, r in res.head(DETAIL_MAX).iterrows():
            sj = "-" if pd.isna(r["sajung"]) else f"{r['sajung']:.3f}%"
            wj = "-" if pd.isna(r["winner_sajung"]) else f"{r['winner_sajung']:.3f}%"
            pc = "-" if pd.isna(r["participants"]) else f"{int(r['participants'])}개사"
            lines.append(f"• {_title(r)}\n  1순위 {_e(r['first_company'])} {_won(r['first_amt'])}\n"
                         f"  실제 사정율 {sj} | 1순위 사정율 {wj} | 참가 {pc}")
            keys.append((r["bid_key"], "result"))
        _more(lines, len(res))
        parts.append("\n".join(lines))

    # 3-1) 추천 성적: 개찰 전에 보낸 추천대로 넣었다면?
    sc = analysis.pick_scorecard(df)
    if not sc.empty:
        sc = sc[sc["bid_key"].isin(t["bid_key"]) & (sc["open_date"] >= now - pd.Timedelta(days=3))
                & (sc["결과"] != "결과 대기")]
        sc = sc[~sc["bid_key"].map(lambda k: (k, "score") in done).astype(bool)]
        if len(sc):
            lines = [f"📈 <b>추천 성적</b> — 추천대로 넣었다면 {_e(analysis.scorecard_summary(sc))}"]
            for _, r in sc.head(DETAIL_MAX).iterrows():
                vr = r["가상 순위"]
                rank = "" if pd.isna(vr) else (" · 상위권 밖" if vr == float("inf") else f" · 가상 {int(vr)}위")
                lines.append(f"• {_e(r['name'])}\n  추천 {r['sajung_pick']:.2f}% → 실제 {r['sajung']:.2f}% / "
                             f"낙찰자 {r['winner_sajung']:.2f}% → <b>{_e(r['결과'])}</b>{rank}")
            keys += [(k, "score") for k in sc["bid_key"]]
            _more(lines, len(sc))
            parts.append("\n".join(lines))

    # 3-2) 한전 새 공고 (공사 · 전기 관련)
    try:
        import kepco
        kb = kepco.load(recent_days=10)
        if not kb.empty:
            reg = db.company()["region"]
            kb = kepco.filter_bids(kb, region=reg, local_only=scope() == "local")
            dl = kb["end_dt"].fillna(kb["attend_close_dt"])
            kb = kb[(dl >= now) & (kb["notice_dt"] >= now - pd.Timedelta(days=7))]
            kb = kb[~kb["bid_no"].map(lambda k: ("KEPCO-" + k, "new") in done).astype(bool)].sort_values("end_dt")
            if len(kb):
                lines = [f"🔌 <b>한전 새 공고 {len(kb)}건</b>"]
                for _, r in kb.head(DETAIL_MAX).iterrows():
                    amt = r["base_amt"] if pd.notna(r["base_amt"]) else r["presumed_price"]
                    st_ = kepco.sajung_stats(kepco.load(recent_days=10), r, reg) if pd.notna(r["base_amt"]) else None
                    line = (f"• {_e(r['name'])}\n  {_e(r['발주사'])} {_e(r['place'] or '')} | "
                            f"{'기초' if pd.notna(r['base_amt']) else '추정'} {_won(amt)}\n"
                            f"  입찰신청마감 {_dt(r['attend_close_dt'])} | 입찰마감 {_dt(r['end_dt'])} | {_e(r['낙찰방법'] or '')}")
                    if st_:
                        line += (f"\n  과거 사정율({_e(st_['기준'])} {st_['n']}건): 보통 {st_['q25']:.2f}~{st_['q75']:.2f}% "
                                 f"(중앙 {st_['중앙']:.2f}%)")
                    lines.append(line)
                keys += [("KEPCO-" + k, "new") for k in kb["bid_no"]]
                _more(lines, len(kb))
                parts.append("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        print("한전 알림 생략:", e)

    # 4) 우리 투찰 결과
    mb = analysis.my_bids_view(df)
    if not mb.empty:
        mb = mb[~mb["결과"].isin(["개찰 전", "결과 대기"]) & (mb["open_date"] >= now - pd.Timedelta(days=3))]
        mb = mb[~mb["bid_key"].map(lambda k: (k, "mine") in done).astype(bool)]
        if len(mb):
            lines = [f"📝 <b>우리 투찰 결과 {len(mb)}건</b>"]
            for _, r in mb.iterrows():
                us = "-" if pd.isna(r["sajung"]) else f"{r['sajung']:.3f}%"
                act = "-" if pd.isna(r["sajung_bid"]) else f"{r['sajung_bid']:.3f}%"
                win = "-" if pd.isna(r["winner_sajung"]) else f"{r['winner_sajung']:.3f}%"
                lines.append(f"• {_title(r)}\n  결과 <b>{_e(r['결과'])}</b> | 우리 {_won(r['amount'])} ({us})\n"
                             f"  실제 사정율 {act} | 1순위 {_e(r['first_company'])} ({win})")
                keys.append((r["bid_key"], "mine"))
            parts.insert(0, "\n".join(lines))

    if not parts:
        return "", []
    footer = f'🔗 <a href="{html.escape(s["app_url"])}">입찰 분석기 열기</a>'
    return "\n\n".join(parts + [footer]), keys


def weekly_report(df: pd.DataFrame | None = None, days: int = 7) -> str:
    """지난 N일 성적: 참가 가능 공고·투찰·결과, 누적 성적."""
    df = analysis.load_dataset() if df is None else df
    now = pd.Timestamp.now()
    since = now - pd.Timedelta(days=days)
    t = analysis.apply_filters(df, keywords=analysis.company_keywords(), eligible_only=True)
    opened = t[(t["open_date"] >= since) & (t["open_date"] < now)]
    mb = analysis.my_bids_view(df)
    lines = [f"📈 <b>주간 성적 ({since:%m/%d}~{now:%m/%d})</b>"]
    sc = analysis.pick_scorecard(df)
    if not sc.empty:
        sc = sc[sc["bid_key"].isin(t["bid_key"]) & (sc["결과"] != "결과 대기")]
        wk_sc = sc[sc["open_date"] >= since]
        if len(wk_sc):
            lines.append("추천대로 넣었다면 (이번 주): " + analysis.scorecard_summary(wk_sc))
        if len(sc) > len(wk_sc):
            lines.append("추천대로 넣었다면 (누적): " + analysis.scorecard_summary(sc))
    if mb.empty:
        lines.append(f"참가 가능 공고 {len(opened)}건 개찰 · 우리 투찰 기록 0건")
        lines.append("※ 투찰하신 공고는 사이트에서 '📝 투찰 기록'을 눌러 두면 결과가 자동으로 집계됩니다.")
        return "\n".join(lines)
    wk = mb[(mb["open_date"] >= since) & (mb["open_date"] < now)]
    mine_nos = set(mb["bid_no"])
    missed = opened[~opened["bid_no"].isin(mine_nos)]
    lines.append(f"참가 가능 공고 {len(opened)}건 개찰 · 우리 투찰 {len(wk)}건 · 안 넣은 공고 {len(missed)}건")

    def counts(x):
        r = x["결과"]
        return {"낙찰": int(r.str.contains("낙찰").sum() - r.str.contains("실패").sum()),
                "1순위": int((r == "🏆 1순위").sum()), "하한 미달": int((r == "하한 미달").sum()),
                "10위 이내": int(((x["my_rank"] > 1) & (x["my_rank"] <= 10)).sum())}
    if len(wk):
        c = counts(wk)
        lines.append(f"이번 주 결과: 🏆 낙찰 {c['낙찰']} · 1순위 {c['1순위']} · 2~10위 {c['10위 이내']} · 하한 미달 {c['하한 미달']}")
        gap = (wk["sajung"] - wk["sajung_bid"]).mean()
        if pd.notna(gap):
            lines.append(f"우리 사정율 − 실제 사정율 평균 {gap:+.3f}%p "
                         + ("(평균보다 높게 넣는 편)" if gap > 0.1 else "(평균보다 낮게 넣는 편)" if gap < -0.1 else "(적정)"))
    done = mb[~mb["결과"].isin(["개찰 전", "결과 대기"])]
    if len(done):
        c = counts(done)
        lines.append(f"누적 {len(done)}건: 🏆 낙찰 {c['낙찰']} · 1순위 {c['1순위']} · 하한 미달 {c['하한 미달']}")
    if len(missed) and len(opened):
        lines.append(f"💡 참가 가능 공고의 {len(missed) / len(opened) * 100:.0f}%를 넣지 않았어요. "
                     "낙찰 횟수는 넣은 공고 수에 비례합니다.")
    return "\n".join(lines)


def send_weekly_if_due() -> str:
    from datetime import date, datetime
    last = db.get_kv("weekly_report_date")
    today = date.today()
    if last:
        try:
            gap = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            gap = 99
        if not (today.weekday() == 0 and gap >= 6 or gap >= 7):
            return "주간 리포트: 아직 아님"
    text = weekly_report()
    sent = send("⚡ 입찰 분석기", text) if channels() else []
    db.set_kv("weekly_report_date", today.isoformat())
    return f"주간 리포트 {'전송' if sent else '작성(채널 없음)'}"


def run(dry_run: bool = False) -> str:
    if not dry_run:
        try:  # 오늘 추천을 먼저 기록 → 알림·사이트가 같은 금액을 보고, 추천이 사소하게 흔들리지 않음
            print(f"추천 기록 {analysis.log_picks(analysis.load_dataset())}건")
        except Exception as e:  # noqa: BLE001
            print("추천 기록 실패:", e)
    text, keys = build()
    if not text:
        return "보낼 새 소식 없음"
    if dry_run:
        print(text)
        return f"(미리보기) {len(keys)}건"
    if not channels():
        return "알림 채널(텔레그램/이메일)이 설정되지 않아 보내지 않음"
    sent = send("⚡ 입찰 알림", text)
    now = db.now()
    db.upsert_many("notified", [{"bid_key": k, "kind": kind, "sent_at": now} for k, kind in keys])
    msg = f"{', '.join(sent)}로 {len(keys)}건 알림"
    db.log("알림", "-", len(keys), msg)
    return msg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--alert", help="경고 메시지 바로 보내기 (자동 수집 실패 등)")
    ap.add_argument("--weekly", action="store_true", help="주간 성적 리포트 미리보기")
    a = ap.parse_args()
    if a.weekly:
        print(weekly_report())
    elif a.alert:
        print(send("🚨 입찰 분석기 경고", html.escape(a.alert)) or "채널 설정 없음")
    elif a.test:
        print(send("⚡ 입찰 분석기 테스트", "알림 연결이 정상입니다 ✅") or "채널 설정 없음")
    else:
        print(run(a.dry_run))
        print(f"알림 단계 DB 내려받은 양(대략): {db.read_mb():,.0f}MB")
