"""Shared business rules: time, suppression, stop rules, caps, start validation, stats."""
import datetime as dt
import json
import re

from . import config, db

UTC = dt.timezone.utc
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

LEAD_STATUSES = ["queued", "active", "replied", "bounced", "unsubscribed", "suppressed",
                 "merge_error", "error", "completed"]


# ---------------------------------------------------------------- time helpers
def utcnow():
    return dt.datetime.now(UTC).replace(microsecond=0)


def ts(value):
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(value):
    if not value:
        return None
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def local_day_start(now):
    """Start of the current day in the server's local timezone (UTC in the container by default)."""
    local = now.astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def local_date(now):
    return now.astimezone().date()


# ---------------------------------------------------------------- validation
def valid_email(value):
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))


def domain_of(email):
    return email.rsplit("@", 1)[-1].lower()


def norm_key(key):
    return re.sub(r"\s+", "_", (key or "").strip().lower())


# ---------------------------------------------------------------- events
def log_event(conn, kind, detail="", lead_id=None, campaign_id=None, email=None, at=None):
    conn.execute(
        "INSERT INTO events(lead_id, campaign_id, email, kind, detail, at) VALUES(?,?,?,?,?,?)",
        (lead_id, campaign_id, email, kind, detail, ts(at or utcnow())),
    )


# ---------------------------------------------------------------- sender identity
def sender_business_name(conn):
    stored = db.get_setting(conn, "sender_business_name")
    return (stored if stored is not None else config.env("SENDER_BUSINESS_NAME", "")).strip()


def global_sender_address(conn):
    """Settings override env. A value saved blank in Settings counts as empty (it does NOT fall back)."""
    stored = db.get_setting(conn, "sender_address")
    return (stored if stored is not None else config.env("SENDER_ADDRESS", "")).strip()


def resolve_sender(conn, mailbox):
    address = (mailbox["sender_address"] or "").strip() or global_sender_address(conn)
    business = sender_business_name(conn) or (mailbox["from_name"] or "").strip()
    return business, address


# ---------------------------------------------------------------- suppression
def is_suppressed(conn, email):
    email = (email or "").lower()
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE email=? OR domain=? LIMIT 1", (email, domain_of(email))
    ).fetchone()
    return row is not None


def add_suppression(conn, email=None, domain=None, source="manual", note=""):
    email = (email or "").strip().lower() or None
    domain = (domain or "").strip().lower().lstrip("@") or None
    if email:
        if conn.execute("SELECT 1 FROM suppression WHERE email=?", (email,)).fetchone():
            return False
    elif domain:
        if conn.execute("SELECT 1 FROM suppression WHERE domain=? AND email IS NULL", (domain,)).fetchone():
            return False
    else:
        return False
    conn.execute(
        "INSERT INTO suppression(email, domain, source, note, created_at) VALUES(?,?,?,?,?)",
        (email, None if email else domain, source, note, ts(utcnow())),
    )
    return True


# ---------------------------------------------------------------- stop rules
STOP_FROM = {
    "replied": ("queued", "active", "completed", "error", "merge_error"),
    "bounced": ("queued", "active", "completed", "error", "merge_error"),
    "unsubscribed": ("queued", "active", "completed", "error", "merge_error", "replied", "suppressed"),
}
EVENT_FOR = {"replied": "reply", "bounced": "bounce_hard", "unsubscribed": "unsubscribe"}


def stop_address(conn, email, new_status, detail="", snippet=None):
    """Stop an address in every campaign. Returns [(lead_id, campaign_id, previous_status)]."""
    email = email.lower()
    allowed = STOP_FROM[new_status]
    marks = ",".join("?" for _ in allowed)
    rows = conn.execute(
        f"SELECT id, campaign_id, status FROM leads WHERE email=? AND status IN ({marks})",
        (email, *allowed),
    ).fetchall()
    now = ts(utcnow())
    changed = []
    for row in rows:
        if snippet is not None:
            conn.execute(
                "UPDATE leads SET status=?, reply_snippet=?, wait_reason=NULL, updated_at=? WHERE id=?",
                (new_status, snippet, now, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE leads SET status=?, wait_reason=NULL, updated_at=? WHERE id=?",
                (new_status, now, row["id"]),
            )
        payload = json.dumps({"prev_status": row["status"], "detail": detail})
        log_event(conn, EVENT_FOR[new_status], payload, row["id"], row["campaign_id"], email)
        changed.append((row["id"], row["campaign_id"], row["status"]))
    return changed


def unsubscribe_lead(conn, lead, source="unsubscribe", note="one-click link"):
    add_suppression(conn, email=lead["email"], source=source, note=note)
    return stop_address(conn, lead["email"], "unsubscribed", detail=note)


def undo_unsubscribe(conn, lead):
    email = lead["email"].lower()
    conn.execute("DELETE FROM suppression WHERE email=? AND source='unsubscribe'", (email,))
    restored = 0
    for row in conn.execute("SELECT id, campaign_id FROM leads WHERE email=? AND status='unsubscribed'", (email,)).fetchall():
        ev = conn.execute(
            "SELECT detail FROM events WHERE lead_id=? AND kind='unsubscribe' ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        prev = "queued"
        if ev:
            try:
                prev = json.loads(ev["detail"]).get("prev_status") or "queued"
            except (ValueError, TypeError):
                pass
        conn.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (prev, ts(utcnow()), row["id"]))
        log_event(conn, "unsubscribe_undo", json.dumps({"restored_status": prev}), row["id"], row["campaign_id"], email)
        restored += 1
    return restored


# ---------------------------------------------------------------- caps
def mailbox_usable(mb):
    return bool(mb["active"]) and not mb["paused_reason"]


def today_sent(conn, mailbox_id, now=None):
    now = now or utcnow()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sends WHERE mailbox_id=? AND status IN ('sent','bounce_hard') AND sent_at>=?",
        (mailbox_id, ts(local_day_start(now))),
    ).fetchone()
    return row["n"]


def effective_cap(mb, now=None):
    now = now or utcnow()
    cap = max(0, min(int(mb["daily_cap"]), config.HARD_MAX_DAILY_CAP))
    if mb["ramp_enabled"]:
        days = 0
        if mb["first_send_date"]:
            first = dt.date.fromisoformat(mb["first_send_date"])
            days = max(0, (local_date(now) - first).days)
        cap = min(cap, config.ramp_start() + config.ramp_step() * days)
    return cap


def remaining_capacity(conn, mb, now=None):
    return max(0, effective_cap(mb, now) - today_sent(conn, mb["id"], now))


# ---------------------------------------------------------------- campaigns
def campaign_mailboxes(conn, campaign_id):
    return conn.execute(
        "SELECT m.* FROM mailboxes m JOIN campaign_mailboxes cm ON cm.mailbox_id=m.id "
        "WHERE cm.campaign_id=? ORDER BY m.id",
        (campaign_id,),
    ).fetchall()


def campaign_steps(conn, campaign_id):
    return conn.execute(
        "SELECT * FROM steps WHERE campaign_id=? ORDER BY position", (campaign_id,)
    ).fetchall()


def validate_start(conn, campaign_id, resume=False):
    errors = []
    mailboxes = campaign_mailboxes(conn, campaign_id)
    if not any(mailbox_usable(m) for m in mailboxes):
        errors.append("Assign at least 1 active (not paused) mailbox to this campaign.")
    if not campaign_steps(conn, campaign_id):
        errors.append("Add at least 1 step (step 1 needs a subject and body).")
    statuses = ("queued", "active") if resume else ("queued",)
    marks = ",".join("?" for _ in statuses)
    queued = conn.execute(
        f"SELECT COUNT(*) AS n FROM leads WHERE campaign_id=? AND status IN ({marks})", (campaign_id, *statuses)
    ).fetchone()["n"]
    if not queued:
        errors.append("Import at least 1 lead that is queued (not suppressed or invalid).")
    for mb in mailboxes:
        _, address = resolve_sender(conn, mb)
        if not address:
            errors.append(
                f"Sender postal address is empty for mailbox '{mb['label']}'. Set it in Settings "
                "(or SENDER_ADDRESS in .env) or as a per-mailbox override. It is required in every email footer."
            )
    if not mailboxes:
        errors.append("No mailboxes assigned, so no sender address can be resolved.")
    if not config.public_base_url():
        errors.append("PUBLIC_BASE_URL is not set in .env. It is required for one-click unsubscribe links.")
    return errors


def enqueue_suppression_check(conn, campaign_id):
    """At enqueue (start/resume): mark queued leads that are now suppressed."""
    count = 0
    for lead in conn.execute(
        "SELECT id, email FROM leads WHERE campaign_id=? AND status IN ('queued','active')", (campaign_id,)
    ).fetchall():
        if is_suppressed(conn, lead["email"]):
            conn.execute("UPDATE leads SET status='suppressed', updated_at=? WHERE id=?", (ts(utcnow()), lead["id"]))
            log_event(conn, "suppressed_skip", "on suppression list at enqueue", lead["id"], campaign_id, lead["email"])
            count += 1
    return count


def campaign_stats(conn, campaign_id):
    def one(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    sent = one("SELECT COUNT(*) FROM sends WHERE campaign_id=? AND status='sent'", campaign_id)
    gone_out = one("SELECT COUNT(*) FROM sends WHERE campaign_id=? AND status IN ('sent','bounce_hard')", campaign_id)
    contacted = one("SELECT COUNT(DISTINCT lead_id) FROM sends WHERE campaign_id=? AND status IN ('sent','bounce_hard')", campaign_id)
    counts = {s: 0 for s in LEAD_STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM leads WHERE campaign_id=? GROUP BY status", (campaign_id,)):
        counts[row["status"]] = row["n"]
    per_step = {
        row["step_pos"]: row["n"]
        for row in conn.execute(
            "SELECT step_pos, COUNT(*) AS n FROM sends WHERE campaign_id=? AND status='sent' GROUP BY step_pos",
            (campaign_id,),
        )
    }
    waiting_cap = one("SELECT COUNT(*) FROM leads WHERE campaign_id=? AND status IN ('queued','active') AND wait_reason='waiting on cap'", campaign_id)
    return {
        "sent": sent,
        "gone_out": gone_out,
        "contacted": contacted,
        "replied": counts["replied"],
        "bounced": counts["bounced"],
        "unsubscribed": counts["unsubscribed"],
        "queued": counts["queued"],
        "active": counts["active"],
        "statuses": counts,
        "total": sum(counts.values()),
        "waiting_cap": waiting_cap,
        "reply_rate": round(100.0 * counts["replied"] / contacted, 1) if contacted else 0.0,
        "bounce_rate": round(100.0 * counts["bounced"] / gone_out, 1) if gone_out else 0.0,
        "per_step": per_step,
    }


def check_bounce_rate(conn, campaign_id):
    """Auto-pause a campaign once >=20 emails went out and the bounce rate exceeds MAX_BOUNCE_RATE."""
    stats = campaign_stats(conn, campaign_id)
    if stats["gone_out"] < 20:
        return False
    rate = stats["bounced"] / stats["gone_out"]
    if rate > config.max_bounce_rate():
        row = conn.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row and row["status"] == "active":
            reason = (
                f"Auto-paused: bounce rate {rate:.1%} after {stats['gone_out']} emails exceeds "
                f"MAX_BOUNCE_RATE {config.max_bounce_rate():.1%}. Clean the list before resuming."
            )
            conn.execute("UPDATE campaigns SET status='paused', paused_reason=? WHERE id=?", (reason, campaign_id))
            log_event(conn, "campaign_paused", reason, campaign_id=campaign_id)
            return True
    return False


def worker_status(conn, now=None):
    now = now or utcnow()
    last = db.get_state(conn, "last_tick")
    last_dt = parse_ts(last) if last else None
    ok = bool(last_dt) and (now - last_dt).total_seconds() <= 3 * config.tick_seconds()
    return last, ok
