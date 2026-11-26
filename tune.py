"""추천 방식 자동 검증·선택 (매주).

최근 90일 '우리 참가 가능 공고'에 대해, 각 방식대로 넣었다면 결과가 어땠는지 시험(백테스트)하고
현재 방식보다 '확실히' 좋은 방식이 있을 때만 바꾼다. (우연한 차이에 흔들리지 않도록 보수적으로)

    python tune.py          # 바로 실행
"""
import json
from datetime import date, datetime

import analysis
import db

MIN_TESTS = 100          # 시험 공고가 이보다 적으면 판단 보류
MIN_RANKED = 50          # 가상 순위 비교 공고가 이만큼 있으면 '10위 이내율'로 비교
SWITCH_RATIO = 1.2       # 현재 방식보다 20% 이상 좋아야 교체
SWITCH_MIN_GAP = 5       # 그리고 건수로도 5건 이상 차이


def due() -> bool:
    last = db.get_kv("strategy_eval_date")
    if not last:
        return True
    try:
        days = (date.today() - datetime.strptime(last, "%Y-%m-%d").date()).days
    except ValueError:
        return True
    return date.today().weekday() == 0 and days >= 6 or days >= 7


def decide(summary, current_label: str) -> tuple[str, str]:
    """(선택된 방식 라벨, 이유)"""
    if summary.empty or summary["시험 공고"].max() < MIN_TESTS:
        n = 0 if summary.empty else int(summary["시험 공고"].max())
        return current_label, f"시험 공고 {n}건으로 아직 부족(최소 {MIN_TESTS}건) → 현재 방식 유지"
    use_rank = "순위 비교 공고" in summary and summary["순위 비교 공고"].max() >= MIN_RANKED
    metric, count = ("10위 이내율(%)", "10위 이내") if use_rank else ("무작위 대비(배)", "1순위")
    summary = summary[summary["방식"] != analysis.ORACLE_LABEL]   # 개찰 후 정보를 쓴 참고 방식은 고르지 않음
    s = summary.set_index("방식")
    if current_label not in s.index:
        return current_label, "현재 방식 결과 없음 → 유지"
    best = s[metric].idxmax()
    cur_v, best_v = s.at[current_label, metric], s.at[best, metric]
    gap = s.at[best, count] - s.at[current_label, count]
    if best != current_label and best_v >= cur_v * SWITCH_RATIO and gap >= SWITCH_MIN_GAP:
        return best, f"{metric} {best_v} vs 현재 {cur_v} ({count} {gap:+}건) → 교체"
    return current_label, f"현재 방식 {metric} {cur_v}, 최고 '{best}' {best_v} → 차이가 확실하지 않아 유지"


def run(progress=None) -> str:
    df = analysis.load_dataset()
    summary, _ = analysis.backtest(df, analysis.company_keywords(), test_days=90, max_tests=300,
                                   eligible_only=True)
    cur_mode = analysis.current_strategy()
    cur_label = analysis.STRATEGIES[cur_mode]
    chosen, reason = decide(summary, cur_label)
    new_mode = analysis.LABEL_TO_MODE.get(chosen, cur_mode)
    db.set_kv("strategy", new_mode)
    db.set_kv("strategy_eval_date", date.today().isoformat())
    db.set_kv("strategy_eval", json.dumps({
        "date": date.today().isoformat(), "reason": reason, "chosen": chosen,
        "summary": json.loads(summary.to_json(orient="records", force_ascii=False)) if len(summary) else [],
    }, ensure_ascii=False))
    msg = f"추천 방식: {chosen} — {reason}"
    db.log("추천 검증(자동)", "-", int(summary["시험 공고"].max()) if len(summary) else 0, msg)
    if progress:
        progress(msg)
    try:
        import html

        import notify
        lines = [f"🔬 <b>주간 추천 방식 검증</b>", html.escape(msg)]
        if len(summary):
            cols = [c for c in ("10위 이내율(%)", "1순위", "무작위 대비(배)", "하한 미달률(%)") if c in summary]
            for _, r in summary.head(6).iterrows():
                lines.append("• " + html.escape(r["방식"]) + ": "
                             + " · ".join(f"{c} {r[c]}" for c in cols))
        if notify.channels():
            notify.send("⚡ 입찰 분석기", "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        msg += f" (알림 실패: {e})"
    return msg


if __name__ == "__main__":
    print(run(print))
