from common import now_iso


def notify(db, user_id, title, body, channels=("in_app",)):
    """in_app is stored as delivered. email/sms are written to the outbox as 'queued';
    plug an SMTP/SMS gateway worker in here (or a cron job reading status='queued')."""
    for ch in channels:
        db.execute(
            "INSERT INTO notifications(user_id, channel, title, body, status, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, ch, title, body, "delivered" if ch == "in_app" else "queued", now_iso()))
