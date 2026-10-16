"""Transparent 'needs academic support' flag: score + human-readable reasons."""
import datetime as dt

from common import fetch_all


def compute_risk(db, student_id, reqs, today: dt.date, stale_days: int):
    score, reasons = 0, []
    overdue = [r for r in reqs if r["urgency"] == "overdue"]
    if overdue:
        score += 2 * len(overdue)
        reasons.append(f"{len(overdue)} overdue item{'s' if len(overdue) > 1 else ''}")
    if any(r["type"] == "fees" for r in overdue):
        score += 1
        reasons.append("fees unpaid past deadline")

    open_q = fetch_all(db, "SELECT created_at FROM queries WHERE student_id=? AND status!='resolved'", (student_id,))
    if open_q:
        reasons.append(f"{len(open_q)} open quer{'ies' if len(open_q) > 1 else 'y'}")
    if len(open_q) >= 2:
        score += 2
    cutoff = (today - dt.timedelta(days=stale_days)).isoformat()
    stale = [q for q in open_q if q["created_at"][:10] <= cutoff]
    if stale:
        score += 1
        reasons.append(f"{len(stale)} query unresolved for {stale_days}+ days")

    since = (today - dt.timedelta(days=14)).isoformat()
    reminders = db.execute(
        "SELECT COUNT(*) c FROM followups WHERE student_id=? AND action='reminder' AND created_at>=?",
        (student_id, since)).fetchone()["c"]
    pending = [r for r in reqs if not r["is_done"]]
    if reminders >= 2 and pending:
        score += 2
        reasons.append(f"{reminders} reminders in 14 days, still {len(pending)} item(s) pending")

    level = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return {"level": level, "score": score, "reasons": reasons}
