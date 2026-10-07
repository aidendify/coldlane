"""Worker logic: send pass (window, due steps, rotation, caps, throttle) and IMAP reply/bounce poll."""
import datetime as dt
import email
import json
import random
import re
from email import policy
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from . import config, core, db, mailer

DSN_STATUS_RE = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
FINAL_RCPT_RE = re.compile(r"^(?:Final|Original)-Recipient:\s*[^;]*;\s*<?([^\s>]+)>?", re.I | re.M)
STATUS_LINE_RE = re.compile(r"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.I | re.M)
MSGID_RE = re.compile(r"<[^<>\s]+>")
OPTOUT_PHRASES = {"unsubscribe", "remove me", "remove", "stop", "unsubscribe me", "please remove me"}


# ================================================================ send window
def in_window(campaign, now):
    try:
        tz = ZoneInfo(campaign["timezone"] or "UTC")
    except Exception:  # noqa: BLE001
        tz = dt.timezone.utc
    local = now.astimezone(tz)
    days = {int(d) for d in (campaign["window_days"] or "").split(",") if d.strip().isdigit()}
    if local.weekday() not in days:
        return False
    return int(campaign["window_start_hour"]) <= local.hour < int(campaign["window_end_hour"])


# ================================================================ send pass
def _gap_active(mb, now):
    nxt = core.parse_ts(mb["next_send_at"])
    return bool(nxt) and nxt > now


def _domain_sends_last_hour(conn, domain, now):
    return conn.execute(
        "SELECT COUNT(*) FROM sends WHERE recipient_domain=? AND status IN ('sent','bounce_hard') AND sent_at>?",
        (domain, core.ts(now - dt.timedelta(hours=1))),
    ).fetchone()[0]


def _set_wait(conn, lead_id, reason):
    conn.execute("UPDATE leads SET wait_reason=? WHERE id=?", (reason, lead_id))


def _cap_wait(conn, lead, now):
    _set_wait(conn, lead["id"], "waiting on cap")
    exists = conn.execute(
        "SELECT 1 FROM events WHERE lead_id=? AND kind='cap_skip' AND at>=? LIMIT 1",
        (lead["id"], core.ts(core.local_day_start(now))),
    ).fetchone()
    if not exists:
        core.log_event(conn, "cap_skip", "all assigned mailboxes reached today's cap", lead["id"],
                       lead["campaign_id"], lead["email"], at=now)


def _pick_mailbox(conn, campaign_id, candidates, now):
    """Most remaining capacity wins; ties broken round-robin after the last used mailbox."""
    best = max(rem for rem, _ in candidates)
    tied = sorted((mb for rem, mb in candidates if rem == best), key=lambda m: m["id"])
    last = db.get_state(conn, f"rr_campaign_{campaign_id}")
    last = int(last) if last and last.isdigit() else 0
    for mb in tied:
        if mb["id"] > last:
            return mb
    return tied[0]


def _schedule_gap(conn, mb_id, now):
    gap = random.uniform(config.min_gap(), config.max_gap())
    conn.execute("UPDATE mailboxes SET next_send_at=? WHERE id=?",
                 (core.ts(now + dt.timedelta(seconds=gap)), mb_id))


def _record_send(conn, lead, step, mb, message_id, now, status, result, attempt):
    conn.execute(
        "INSERT INTO sends(campaign_id, lead_id, step_id, step_pos, mailbox_id, recipient, recipient_domain, "
        "message_id, sent_at, status, smtp_result, attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead["campaign_id"], lead["id"], step["id"], step["position"], mb["id"], lead["email"],
         core.domain_of(lead["email"]), message_id, core.ts(now), status, result, attempt),
    )


def _mailbox_failure(conn, mb, err, now):
    fails = (mb["fail_count"] or 0) + 1
    paused = None
    if fails >= 3:
        paused = f"Auto-paused after {fails} consecutive SMTP failures. Last error: {err}"
    conn.execute(
        "UPDATE mailboxes SET fail_count=?, last_error=?, last_error_at=?, paused_reason=COALESCE(?, paused_reason) WHERE id=?",
        (fails, err, core.ts(now), paused, mb["id"]),
    )
    _schedule_gap(conn, mb["id"], now)


def try_send_lead(conn, campaign, steps, lead_id, now):
    """Attempt the next step for one lead. Returns True if an email was accepted by SMTP."""
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead is None or lead["status"] not in ("queued", "active"):
        return False
    if lead["current_step"] >= len(steps):
        conn.execute("UPDATE leads SET status='completed', wait_reason=NULL, updated_at=? WHERE id=?",
                     (core.ts(now), lead["id"]))
        return False
    nxt = core.parse_ts(lead["next_attempt_at"])
    if nxt and nxt > now:
        return False
    step = steps[lead["current_step"]]
    if lead["status"] == "active":
        last = core.parse_ts(lead["last_sent_at"])
        if last and now < last + dt.timedelta(minutes=int(step["delay_minutes"])):
            return False

    # Suppression is re-checked immediately before every send.
    if core.is_suppressed(conn, lead["email"]):
        conn.execute("UPDATE leads SET status='suppressed', wait_reason=NULL, updated_at=? WHERE id=?",
                     (core.ts(now), lead["id"]))
        core.log_event(conn, "suppressed_skip", "on suppression list at send time", lead["id"],
                       lead["campaign_id"], lead["email"], at=now)
        return False

    domain = core.domain_of(lead["email"])
    limit = config.domain_per_hour()
    if _domain_sends_last_hour(conn, domain, now) >= limit:
        _set_wait(conn, lead["id"], f"domain throttle ({limit}/hour for {domain})")
        return False

    if lead["mailbox_id"]:
        mb = conn.execute("SELECT * FROM mailboxes WHERE id=?", (lead["mailbox_id"],)).fetchone()
        if mb is None or not core.mailbox_usable(mb):
            _set_wait(conn, lead["id"], "assigned mailbox paused or inactive")
            return False
        if core.remaining_capacity(conn, mb, now) <= 0:
            _cap_wait(conn, lead, now)
            return False
        if _gap_active(mb, now):
            return False
    else:
        usable = [m for m in core.campaign_mailboxes(conn, campaign["id"]) if core.mailbox_usable(m)]
        if not usable:
            _set_wait(conn, lead["id"], "no active mailbox")
            return False
        with_cap = [(core.remaining_capacity(conn, m, now), m) for m in usable]
        with_cap = [(rem, m) for rem, m in with_cap if rem > 0]
        if not with_cap:
            _cap_wait(conn, lead, now)
            return False
        available = [(rem, m) for rem, m in with_cap if not _gap_active(m, now)]
        if not available:
            return False
        mb = _pick_mailbox(conn, campaign["id"], available, now)

    fields = json.loads(lead["fields_json"] or "{}")
    fields.setdefault("email", lead["email"])
    try:
        body = mailer.render(step["body"], fields)
        if (step["subject"] or "").strip():
            subject = mailer.render(step["subject"], fields)
        else:
            base = lead["thread_subject"] or mailer.render(steps[0]["subject"], fields)
            subject = base if base.lower().startswith("re:") else f"Re: {base}"
    except mailer.MergeError as exc:
        conn.execute("UPDATE leads SET status='merge_error', wait_reason=?, updated_at=? WHERE id=?",
                     (str(exc), core.ts(now), lead["id"]))
        core.log_event(conn, "merge_error", str(exc), lead["id"], lead["campaign_id"], lead["email"], at=now)
        return False

    business, address = core.resolve_sender(conn, mb)
    base_url = config.public_base_url()
    if not address or not base_url:
        _set_wait(conn, lead["id"], "missing sender address or PUBLIC_BASE_URL")
        return False
    unsub_url = f"{base_url}/u/{lead['unsub_token']}"
    in_reply_to = lead["last_message_id"] if lead["current_step"] > 0 else None
    references = (lead["thread_refs"] or "").strip() or None
    msg, message_id = mailer.build_message(mb, lead["email"], subject, body, unsub_url, business, address,
                                           in_reply_to=in_reply_to, references=references, now=now)
    attempt = (lead["attempts"] or 0) + 1
    try:
        result = mailer.send_message(mb, msg, lead["email"])
    except mailer.MailboxError as exc:
        _record_send(conn, lead, step, mb, message_id, now, "mailbox_error", str(exc), attempt)
        _mailbox_failure(conn, mb, str(exc), now)
        return False
    except mailer.HardBounce as exc:
        _record_send(conn, lead, step, mb, message_id, now, "bounce_hard", str(exc), attempt)
        conn.execute("UPDATE leads SET mailbox_id=? WHERE id=?", (mb["id"], lead["id"]))
        conn.execute("UPDATE mailboxes SET fail_count=0 WHERE id=?", (mb["id"],))
        _schedule_gap(conn, mb["id"], now)
        core.add_suppression(conn, email=lead["email"], source="bounce", note=str(exc)[:200])
        core.stop_address(conn, lead["email"], "bounced", detail=str(exc))
        core.check_bounce_rate(conn, campaign["id"])
        return False
    except mailer.SoftFail as exc:
        _record_send(conn, lead, step, mb, message_id, now, "retry", str(exc), attempt)
        _schedule_gap(conn, mb["id"], now)
        if attempt >= 3:
            conn.execute("UPDATE leads SET status='error', mailbox_id=?, attempts=?, wait_reason=?, updated_at=? WHERE id=?",
                         (mb["id"], attempt, f"failed after 3 attempts: {exc}"[:300], core.ts(now), lead["id"]))
            core.log_event(conn, "error", str(exc), lead["id"], lead["campaign_id"], lead["email"], at=now)
        else:
            conn.execute(
                "UPDATE leads SET mailbox_id=?, attempts=?, wait_reason=?, next_attempt_at=?, updated_at=? WHERE id=?",
                (mb["id"], attempt, f"retry {attempt}/3: {exc}"[:300],
                 core.ts(now + dt.timedelta(seconds=120 * attempt)), core.ts(now), lead["id"]))
            core.log_event(conn, "bounce_soft", str(exc), lead["id"], lead["campaign_id"], lead["email"], at=now)
        return False

    # Success
    _record_send(conn, lead, step, mb, message_id, now, "sent", result, attempt)
    new_step = lead["current_step"] + 1
    status = "completed" if new_step >= len(steps) else "active"
    refs = ((lead["thread_refs"] or "") + " " + message_id).strip()
    conn.execute(
        "UPDATE leads SET status=?, mailbox_id=?, current_step=?, last_sent_at=?, last_message_id=?, thread_refs=?, "
        "thread_subject=COALESCE(thread_subject, ?), attempts=0, next_attempt_at=NULL, wait_reason=NULL, updated_at=? "
        "WHERE id=?",
        (status, mb["id"], new_step, core.ts(now), message_id, refs, subject, core.ts(now), lead["id"]),
    )
    conn.execute(
        "UPDATE mailboxes SET fail_count=0, first_send_date=COALESCE(first_send_date, ?) WHERE id=?",
        (core.local_date(now).isoformat(), mb["id"]),
    )
    _schedule_gap(conn, mb["id"], now)
    db.set_state(conn, f"rr_campaign_{campaign['id']}", mb["id"])
    return True


def run_campaign(conn, campaign, now):
    steps = core.campaign_steps(conn, campaign["id"])
    if not steps:
        return 0
    followups = [r["id"] for r in conn.execute(
        "SELECT id FROM leads WHERE campaign_id=? AND status='active' ORDER BY last_sent_at, id", (campaign["id"],))]
    new = [r["id"] for r in conn.execute(
        "SELECT id FROM leads WHERE campaign_id=? AND status='queued' ORDER BY id", (campaign["id"],))]
    sent = 0
    for lead_id in followups + new:  # due follow-ups take priority over new leads
        status = conn.execute("SELECT status FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()["status"]
        if status != "active":
            break
        if try_send_lead(conn, campaign, steps, lead_id, now):
            sent += 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND status IN ('queued','active')", (campaign["id"],)
    ).fetchone()[0]
    if remaining == 0:
        conn.execute("UPDATE campaigns SET status='completed' WHERE id=? AND status='active'", (campaign["id"],))
    return sent


def send_pass(conn, now=None):
    now = now or core.utcnow()
    total = 0
    for campaign in conn.execute("SELECT * FROM campaigns WHERE status='active' ORDER BY id").fetchall():
        if not in_window(campaign, now):
            continue
        total += run_campaign(conn, campaign, now)
    return total


# ================================================================ IMAP classification
def _text_body(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        if part.get_content_type() == "text/html":
            content = re.sub(r"<[^>]+>", " ", content)
        return content
    except Exception:  # noqa: BLE001
        return ""


def _snippet(text):
    lines = []
    for line in text.splitlines():
        if line.strip().startswith(">"):
            continue
        if re.match(r"^\s*On .+wrote:\s*$", line):
            break
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:300]


def _msgids(value):
    return MSGID_RE.findall(value or "")


def parse_incoming(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    from_addr = parseaddr(str(msg.get("From", "")))[1].lower()
    local = from_addr.split("@")[0]
    ids = _msgids(str(msg.get("In-Reply-To", ""))) + _msgids(str(msg.get("References", "")))
    auto = False
    auto_sub = str(msg.get("Auto-Submitted", "")).strip().lower()
    if auto_sub and auto_sub != "no":
        auto = True
    if msg.get("X-Autoreply") is not None or msg.get("X-Autorespond") is not None:
        auto = True
    if str(msg.get("Precedence", "")).strip().lower() == "auto_reply":
        auto = True

    is_dsn = (msg.get_content_type() == "multipart/report"
              and str(msg.get_param("report-type", "")).lower() == "delivery-status")
    is_daemon = local in ("mailer-daemon", "postmaster")
    dsn = []
    original_ids = []
    if is_dsn or is_daemon:
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "message/delivery-status":
                blocks = part.get_payload()
                if isinstance(blocks, list):
                    for block in blocks:
                        rcpt = block.get("Final-Recipient") or block.get("Original-Recipient")
                        status = block.get("Status")
                        if rcpt and status:
                            addr = str(rcpt).split(";", 1)[-1].strip().strip("<>").lower()
                            dsn.append((addr, str(status).strip()))
                else:
                    text = str(blocks)
                    for rcpt, status in zip(FINAL_RCPT_RE.findall(text), STATUS_LINE_RE.findall(text)):
                        dsn.append((rcpt.lower(), status))
            elif ctype in ("text/rfc822-headers", "message/rfc822"):
                found = re.search(r"^Message-ID:.*$", part.as_string(), re.I | re.M)
                if found:
                    original_ids.extend(_msgids(found.group(0)))
        if not dsn:
            whole = msg.as_string()
            rcpts = FINAL_RCPT_RE.findall(whole)
            statuses = STATUS_LINE_RE.findall(whole)
            for i, rcpt in enumerate(rcpts):
                dsn.append((rcpt.lower(), statuses[i] if i < len(statuses) else (statuses[0] if statuses else "")))
    body = _text_body(msg)
    return {
        "from": from_addr,
        "subject": str(msg.get("Subject", "")),
        "ids": ids,
        "auto_reply": auto,
        "is_bounce": is_dsn or is_daemon,
        "dsn": dsn,
        "original_ids": original_ids,
        "body": body,
        "snippet": _snippet(body),
        "raw_text": msg.as_string() if (is_dsn or is_daemon) else "",
    }


def _lead_by_message_ids(conn, ids):
    for mid in ids:
        row = conn.execute(
            "SELECT l.* FROM sends s JOIN leads l ON l.id=s.lead_id WHERE s.message_id=? ORDER BY s.id DESC LIMIT 1",
            (mid,),
        ).fetchone()
        if row:
            return row
    return None


def _lead_by_from(conn, address, statuses=("active", "queued", "completed")):
    marks = ",".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM leads WHERE email=? AND status IN ({marks}) ORDER BY last_sent_at DESC, id DESC LIMIT 1",
        (address, *statuses),
    ).fetchone()


def process_incoming(conn, raw, mailbox_id=None):
    """Classify one inbound message and apply stop rules. Returns a short classification string."""
    info = parse_incoming(raw)
    if info["is_bounce"]:
        results = []
        pairs = list(info["dsn"])
        if not pairs and info["original_ids"]:
            lead = _lead_by_message_ids(conn, info["original_ids"])
            codes = DSN_STATUS_RE.findall(info["raw_text"])
            if lead and codes:
                pairs.append((lead["email"], ".".join(codes[0])))
        if not pairs:
            # Non-standard bounce: look for any lead address and an enhanced status code in the text.
            codes = DSN_STATUS_RE.findall(info["raw_text"])
            for addr in set(a.lower() for a in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", info["raw_text"])):
                if codes and conn.execute("SELECT 1 FROM leads WHERE email=? LIMIT 1", (addr,)).fetchone():
                    pairs.append((addr, ".".join(codes[0])))
        for addr, status in pairs:
            lead = conn.execute("SELECT * FROM leads WHERE email=? ORDER BY last_sent_at DESC, id DESC LIMIT 1",
                                (addr,)).fetchone()
            if lead is None:
                continue
            if status.startswith("5"):
                core.add_suppression(conn, email=addr, source="bounce", note=f"DSN {status}")
                changed = core.stop_address(conn, addr, "bounced", detail=f"DSN {status}")
                for cid in {c for _, c, _ in changed}:
                    core.check_bounce_rate(conn, cid)
                results.append(f"bounce_hard:{addr}")
            else:
                core.log_event(conn, "bounce_soft", f"DSN {status} (logged only)", lead["id"], lead["campaign_id"], addr)
                results.append(f"bounce_soft:{addr}")
        return ",".join(results) or "bounce_unmatched"

    lead = _lead_by_message_ids(conn, info["ids"]) or _lead_by_from(conn, info["from"])
    if lead is None:
        return "ignored"
    if info["auto_reply"]:
        core.log_event(conn, "auto_reply", json.dumps({"subject": info["subject"], "snippet": info["snippet"]}),
                       lead["id"], lead["campaign_id"], lead["email"])
        return "auto_reply"
    stripped = re.sub(r"[\s.!]+", " ", info["snippet"]).strip().lower()
    subject_l = info["subject"].strip().lower()
    if stripped in OPTOUT_PHRASES or (subject_l == "unsubscribe" and len(stripped) < 40):
        core.add_suppression(conn, email=lead["email"], source="reply_optout", note="opt-out reply")
        core.stop_address(conn, lead["email"], "unsubscribed", detail="opt-out reply")
        return "unsubscribe"
    changed = core.stop_address(conn, lead["email"], "replied", detail=info["subject"][:200], snippet=info["snippet"])
    if not changed and lead["status"] == "replied":
        return "reply_duplicate"
    return "reply"


def poll_mailbox(conn, mb):
    client = mailer.imap_open(mb)
    processed = 0
    try:
        typ, data = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError(f"SELECT INBOX failed: {data}")
        uidvalidity = None
        resp = client.untagged_responses.get("UIDVALIDITY")
        if resp:
            uidvalidity = resp[0].decode() if isinstance(resp[0], bytes) else str(resp[0])
        last_uid = mb["last_imap_uid"]
        if uidvalidity and mb["imap_uidvalidity"] and uidvalidity != mb["imap_uidvalidity"]:
            last_uid = None
        if last_uid is None:
            created = core.parse_ts(mb["created_at"]) or core.utcnow()
            since = (created - dt.timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = client.uid("SEARCH", None, "SINCE", since)
        else:
            typ, data = client.uid("SEARCH", None, f"UID {int(last_uid) + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split()) if typ == "OK" else []
        max_uid = int(last_uid or 0)
        for uid in uids:
            if last_uid is not None and uid <= int(last_uid):
                continue
            typ, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = None
            for item in fetched or []:
                if isinstance(item, tuple) and len(item) > 1:
                    raw = item[1]
                    break
            if raw:
                process_incoming(conn, raw, mb["id"])
                processed += 1
            max_uid = max(max_uid, uid)
        conn.execute("UPDATE mailboxes SET last_imap_uid=?, imap_uidvalidity=COALESCE(?, imap_uidvalidity) WHERE id=?",
                     (max_uid, uidvalidity, mb["id"]))
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    return processed


def imap_pass(conn, now=None, force=False):
    now = now or core.utcnow()
    total = 0
    for mb in conn.execute("SELECT * FROM mailboxes WHERE active=1 ORDER BY id").fetchall():
        last = core.parse_ts(mb["last_imap_poll"])
        if not force and last and (now - last).total_seconds() < config.imap_poll_seconds():
            continue
        conn.execute("UPDATE mailboxes SET last_imap_poll=? WHERE id=?", (core.ts(now), mb["id"]))
        try:
            total += poll_mailbox(conn, mb)
        except Exception as exc:  # noqa: BLE001
            conn.execute("UPDATE mailboxes SET last_error=?, last_error_at=? WHERE id=?",
                         (f"IMAP: {exc.__class__.__name__}: {exc}"[:500], core.ts(now), mb["id"]))
    return total


def tick(conn, now=None):
    now = now or core.utcnow()
    db.set_state(conn, "last_tick", core.ts(now))
    sent = send_pass(conn, now)
    polled = imap_pass(conn, now)
    db.set_state(conn, "last_tick", core.ts(core.utcnow()))
    return sent, polled
