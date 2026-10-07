"""Flask web UI. Run with: gunicorn 'coldlane.web:create_app()'."""
import csv
import hmac
import io
import json
import secrets
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from . import config, core, crypto, db, mailer

PUBLIC_ENDPOINTS = {"health", "login", "static", "unsubscribe", "unsubscribe_undo"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UNIT_MINUTES = {"minutes": 1, "hours": 60, "days": 1440}
MAX_STEPS = 5


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _csv_response(rows, header, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _read_upload(file_storage):
    raw = file_storage.read()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _delay_display(minutes):
    if minutes and minutes % 1440 == 0:
        return minutes // 1440, "days"
    if minutes and minutes % 60 == 0:
        return minutes // 60, "hours"
    return minutes, "minutes"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.env("SECRET_KEY") or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    db.init_db()
    crypto.get_key()  # fail fast on an invalid ENCRYPTION_KEY

    @app.teardown_appcontext
    def close_db(exc):  # noqa: ARG001
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not session.get("owner"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.context_processor
    def inject_globals():
        ctx = {"day_names": DAY_NAMES}
        if session.get("owner"):
            conn = get_db()
            _, ok = core.worker_status(conn)
            ctx["worker_ok"] = ok
            ctx["generated_key"] = crypto.using_generated_key()
            ctx["public_base_url"] = config.public_base_url()
        return ctx

    # ------------------------------------------------------------ public
    @app.get("/health")
    def health():
        conn = get_db()
        last, ok = core.worker_status(conn)
        active = conn.execute(
            "SELECT COUNT(*) FROM mailboxes WHERE active=1 AND paused_reason IS NULL").fetchone()[0]
        return jsonify({"status": "ok", "worker_last_tick": last, "worker_ok": ok, "mailboxes_active": active})

    @app.route("/u/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        conn = get_db()
        lead = conn.execute("SELECT * FROM leads WHERE unsub_token=?", (token,)).fetchone()
        if lead is None:
            abort(404)
        core.unsubscribe_lead(conn, lead, note="one-click POST" if request.method == "POST" else "unsubscribe link")
        if request.method == "POST":
            return Response("Unsubscribed.\n", status=200, mimetype="text/plain")
        return render_template("unsubscribed.html", token=token, email=lead["email"],
                               business=core.sender_business_name(conn))

    @app.post("/u/<token>/undo")
    def unsubscribe_undo(token):
        conn = get_db()
        lead = conn.execute("SELECT * FROM leads WHERE unsub_token=?", (token,)).fetchone()
        if lead is None:
            abort(404)
        core.undo_unsubscribe(conn, lead)
        return render_template("unsubscribe_undone.html", token=token, email=lead["email"])

    # ------------------------------------------------------------ auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        configured = bool(config.env("OWNER_PASSWORD"))
        if request.method == "POST":
            password = request.form.get("password", "")
            expected = config.env("OWNER_PASSWORD", "")
            if configured and hmac.compare_digest(password.encode(), expected.encode()):
                session.clear()
                session["owner"] = True
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Wrong password.", "error")
        return render_template("login.html", configured=configured)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------ dashboard
    @app.get("/")
    def dashboard():
        conn = get_db()
        campaigns = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        rows = [(c, core.campaign_stats(conn, c["id"])) for c in campaigns]
        last, ok = core.worker_status(conn)
        totals = {k: sum(s[k] for _, s in rows) for k in ("sent", "replied", "bounced", "unsubscribed", "queued")}
        return render_template("dashboard.html", rows=rows, last_tick=last, totals=totals)

    # ------------------------------------------------------------ mailboxes
    def _mailbox_form(form, existing=None):
        errors = []
        data = {}
        for key in ("label", "from_name", "from_email", "smtp_host", "smtp_user", "imap_host", "imap_user",
                    "sender_address", "signature"):
            data[key] = (form.get(key) or "").strip()
        data["from_email"] = data["from_email"].lower()
        if not data["label"]:
            data["label"] = data["from_email"]
        if not core.valid_email(data["from_email"]):
            errors.append("From email is not a valid address.")
        if not data["smtp_host"]:
            errors.append("SMTP host is required.")
        if not data["imap_host"]:
            data["imap_host"] = data["smtp_host"]
        if not data["imap_user"]:
            data["imap_user"] = data["smtp_user"]
        data["smtp_security"] = form.get("smtp_security", "starttls")
        if data["smtp_security"] not in ("starttls", "ssl", "none"):
            errors.append("SMTP security must be starttls, ssl or none.")
        for key, default in (("smtp_port", 587), ("imap_port", 993)):
            try:
                data[key] = int(form.get(key) or default)
                if not 1 <= data[key] <= 65535:
                    raise ValueError
            except ValueError:
                errors.append(f"{key.replace('_', ' ').upper()} must be a port number.")
                data[key] = default
        data["imap_ssl"] = 1 if form.get("imap_ssl") else 0
        data["ramp_enabled"] = 1 if form.get("ramp_enabled") else 0
        data["active"] = 1 if form.get("active") else 0
        try:
            cap = int(form.get("daily_cap") or config.default_daily_cap())
        except ValueError:
            cap = -1
        if cap < 1 or cap > config.HARD_MAX_DAILY_CAP:
            errors.append(f"Daily cap must be between 1 and {config.HARD_MAX_DAILY_CAP} "
                          f"(hard maximum {config.HARD_MAX_DAILY_CAP}). Got: {form.get('daily_cap')}")
        data["daily_cap"] = cap
        smtp_pw = form.get("smtp_password") or ""
        imap_pw = form.get("imap_password") or ""
        if existing is None:
            data["smtp_pass_enc"] = crypto.encrypt(smtp_pw)
            data["imap_pass_enc"] = crypto.encrypt(imap_pw or smtp_pw)
        else:
            data["smtp_pass_enc"] = crypto.encrypt(smtp_pw) if smtp_pw else existing["smtp_pass_enc"]
            data["imap_pass_enc"] = crypto.encrypt(imap_pw) if imap_pw else existing["imap_pass_enc"]
        return data, errors

    @app.get("/mailboxes")
    def mailboxes():
        conn = get_db()
        rows = []
        for mb in conn.execute("SELECT * FROM mailboxes ORDER BY id").fetchall():
            rows.append((mb, core.today_sent(conn, mb["id"]), core.effective_cap(mb), core.resolve_sender(conn, mb)))
        return render_template("mailboxes.html", rows=rows)

    @app.route("/mailboxes/new", methods=["GET", "POST"])
    def mailbox_new():
        conn = get_db()
        if request.method == "POST":
            data, errors = _mailbox_form(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("mailbox_form.html", mb=request.form, is_new=True), 400
            data["created_at"] = core.ts(core.utcnow())
            cols = ",".join(data.keys())
            marks = ",".join("?" for _ in data)
            cur = conn.execute(f"INSERT INTO mailboxes({cols}) VALUES({marks})", tuple(data.values()))
            flash("Mailbox saved. Use Test connection to verify SMTP and IMAP.", "ok")
            return redirect(url_for("mailbox_edit", mailbox_id=cur.lastrowid))
        defaults = {"daily_cap": config.default_daily_cap(), "ramp_enabled": 1, "active": 1, "smtp_port": 587,
                    "imap_port": 993, "imap_ssl": 1, "smtp_security": "starttls"}
        return render_template("mailbox_form.html", mb=defaults, is_new=True)

    @app.route("/mailboxes/<int:mailbox_id>", methods=["GET", "POST"])
    def mailbox_edit(mailbox_id):
        conn = get_db()
        mb = conn.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        if mb is None:
            abort(404)
        if request.method == "POST":
            data, errors = _mailbox_form(request.form, existing=mb)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("mailbox_form.html", mb=request.form, is_new=False, mailbox_id=mailbox_id), 400
            if request.form.get("clear_pause"):
                data["paused_reason"] = None
                data["fail_count"] = 0
            sets = ",".join(f"{k}=?" for k in data)
            conn.execute(f"UPDATE mailboxes SET {sets} WHERE id=?", (*data.values(), mailbox_id))
            flash("Mailbox updated.", "ok")
            return redirect(url_for("mailbox_edit", mailbox_id=mailbox_id))
        return render_template("mailbox_form.html", mb=mb, is_new=False, mailbox_id=mailbox_id,
                               today=core.today_sent(conn, mailbox_id), cap=core.effective_cap(mb))

    @app.post("/mailboxes/<int:mailbox_id>/test")
    def mailbox_test(mailbox_id):
        conn = get_db()
        mb = conn.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        if mb is None:
            abort(404)
        ok, results = mailer.test_connection(mb)
        for line in results:
            flash(line, "ok" if " OK" in line else "error")
        if ok:
            conn.execute("UPDATE mailboxes SET fail_count=0, paused_reason=NULL WHERE id=?", (mailbox_id,))
            flash("Connection test passed.", "ok")
        else:
            conn.execute("UPDATE mailboxes SET last_error=?, last_error_at=? WHERE id=?",
                         ("; ".join(r for r in results if "FAILED" in r)[:500], core.ts(core.utcnow()), mailbox_id))
        return redirect(url_for("mailbox_edit", mailbox_id=mailbox_id))

    # ------------------------------------------------------------ campaigns
    def _campaign_form(form):
        errors = []
        data = {"name": (form.get("name") or "").strip() or "Untitled campaign",
                "timezone": (form.get("timezone") or "UTC").strip()}
        try:
            ZoneInfo(data["timezone"])
        except Exception:  # noqa: BLE001
            errors.append(f"Unknown timezone '{data['timezone']}'. Use an IANA name like America/New_York or UTC.")
        days = sorted({int(d) for d in form.getlist("window_days") if d.isdigit() and 0 <= int(d) <= 6})
        if not days:
            errors.append("Pick at least one send day.")
        data["window_days"] = ",".join(str(d) for d in days)
        try:
            start = int(form.get("window_start_hour", 9))
            end = int(form.get("window_end_hour", 17))
        except ValueError:
            start, end = 9, 17
            errors.append("Window hours must be numbers.")
        if not (0 <= start < end <= 24):
            errors.append("Send window must satisfy 0 <= start hour < end hour <= 24.")
        data["window_start_hour"], data["window_end_hour"] = start, end
        mailbox_ids = [int(m) for m in form.getlist("mailboxes") if m.isdigit()]
        steps = []
        for pos in range(1, MAX_STEPS + 1):
            subject = (form.get(f"subject_{pos}") or "").strip()
            body = (form.get(f"body_{pos}") or "").replace("\r\n", "\n").strip()
            if not subject and not body:
                continue
            if not body:
                errors.append(f"Step {pos} needs a body.")
            try:
                amount = int(form.get(f"delay_{pos}") or 0)
            except ValueError:
                amount = 0
            unit = form.get(f"delay_unit_{pos}", "days")
            minutes = max(0, amount) * UNIT_MINUTES.get(unit, 1440)
            steps.append({"position": pos, "subject": subject, "body": body, "delay_minutes": minutes})
        positions = [s["position"] for s in steps]
        if positions and positions != list(range(1, len(positions) + 1)):
            errors.append("Steps must be filled in order without gaps (step 1, 2, 3...).")
        if steps:
            steps[0]["delay_minutes"] = 0
            if not steps[0]["subject"]:
                errors.append("Step 1 needs a subject.")
        for s in steps:
            for text in (s["subject"], s["body"]):
                if text.count("{{") != text.count("}}"):
                    errors.append(f"Step {s['position']} has unbalanced {{{{ }}}} merge braces.")
        return data, mailbox_ids, steps, errors

    def _save_campaign(conn, campaign_id, data, mailbox_ids, steps):
        sets = ",".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE campaigns SET {sets} WHERE id=?", (*data.values(), campaign_id))
        conn.execute("DELETE FROM campaign_mailboxes WHERE campaign_id=?", (campaign_id,))
        for mid in mailbox_ids:
            conn.execute("INSERT OR IGNORE INTO campaign_mailboxes(campaign_id, mailbox_id) VALUES(?,?)",
                         (campaign_id, mid))
        existing = {s["position"]: s for s in core.campaign_steps(conn, campaign_id)}
        for s in steps:
            if s["position"] in existing:
                conn.execute("UPDATE steps SET subject=?, body=?, delay_minutes=? WHERE id=?",
                             (s["subject"], s["body"], s["delay_minutes"], existing[s["position"]]["id"]))
            else:
                conn.execute("INSERT INTO steps(campaign_id, position, subject, body, delay_minutes) VALUES(?,?,?,?,?)",
                             (campaign_id, s["position"], s["subject"], s["body"], s["delay_minutes"]))
        keep = {s["position"] for s in steps}
        for pos, row in existing.items():
            if pos not in keep:
                conn.execute("DELETE FROM steps WHERE id=?", (row["id"],))

    def _campaign_context(conn, campaign_id):
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            abort(404)
        steps = {s["position"]: s for s in core.campaign_steps(conn, campaign_id)}
        step_rows = []
        for pos in range(1, MAX_STEPS + 1):
            s = steps.get(pos)
            amount, unit = _delay_display(s["delay_minutes"] if s else (0 if pos == 1 else 3))
            if not s and pos > 1:
                unit = "days"
            step_rows.append({"position": pos, "subject": s["subject"] if s else "", "body": s["body"] if s else "",
                              "delay": amount, "unit": unit})
        assigned = {m["id"] for m in core.campaign_mailboxes(conn, campaign_id)}
        return {
            "campaign": campaign,
            "steps": step_rows,
            "all_mailboxes": conn.execute("SELECT * FROM mailboxes ORDER BY id").fetchall(),
            "assigned": assigned,
            "days": {int(d) for d in campaign["window_days"].split(",") if d.isdigit()},
            "stats": core.campaign_stats(conn, campaign_id),
            "imports": conn.execute("SELECT * FROM imports WHERE campaign_id=? ORDER BY id DESC", (campaign_id,)).fetchall(),
            "start_errors": core.validate_start(conn, campaign_id) if campaign["status"] in ("draft", "completed") else [],
        }

    @app.route("/campaigns/new", methods=["GET", "POST"])
    def campaign_new():
        conn = get_db()
        if request.method == "POST":
            data, mailbox_ids, steps, errors = _campaign_form(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("campaign_new"))
            cur = conn.execute("INSERT INTO campaigns(name, created_at) VALUES(?, ?)", (data["name"], core.ts(core.utcnow())))
            _save_campaign(conn, cur.lastrowid, data, mailbox_ids, steps)
            flash("Campaign created. Import leads next.", "ok")
            return redirect(url_for("campaign_detail", campaign_id=cur.lastrowid))
        return render_template("campaign_new.html", all_mailboxes=conn.execute("SELECT * FROM mailboxes ORDER BY id").fetchall())

    @app.route("/campaigns/<int:campaign_id>", methods=["GET", "POST"])
    def campaign_detail(campaign_id):
        conn = get_db()
        if request.method == "POST":
            if conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone() is None:
                abort(404)
            data, mailbox_ids, steps, errors = _campaign_form(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                _save_campaign(conn, campaign_id, data, mailbox_ids, steps)
                flash("Campaign saved.", "ok")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id))
        return render_template("campaign.html", **_campaign_context(conn, campaign_id))

    @app.post("/campaigns/<int:campaign_id>/import")
    def campaign_import(campaign_id):
        conn = get_db()
        if conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone() is None:
            abort(404)
        if not request.form.get("lawful_basis"):
            flash("Import rejected: tick the lawful-basis confirmation (\"I confirm I have a lawful basis to email "
                  "these contacts and will honor opt-outs.\").", "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id)), 303
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id)), 303
        reader = csv.DictReader(io.StringIO(_read_upload(upload)))
        if not reader.fieldnames or "email" not in [core.norm_key(f) for f in reader.fieldnames]:
            flash("Import rejected: the CSV needs an 'email' column.", "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id)), 303
        acknowledged_by = (request.form.get("acknowledged_by") or "owner").strip()[:200]
        now = core.ts(core.utcnow())
        cur = conn.execute(
            "INSERT INTO imports(campaign_id, filename, acknowledged_at, acknowledged_by) VALUES(?,?,?,?)",
            (campaign_id, upload.filename[:255], now, acknowledged_by))
        import_id = cur.lastrowid
        existing = {r["email"] for r in conn.execute("SELECT email FROM leads WHERE campaign_id=?", (campaign_id,))}
        counts = {"rows": 0, "accepted": 0, "suppressed": 0, "invalid": 0, "duplicates": 0}
        for row in reader:
            counts["rows"] += 1
            fields = {core.norm_key(k): (v or "").strip() for k, v in row.items() if k}
            email_addr = fields.pop("email", "").strip().lower()
            if not core.valid_email(email_addr):
                counts["invalid"] += 1
                continue
            if email_addr in existing:
                counts["duplicates"] += 1
                continue
            existing.add(email_addr)
            status = "suppressed" if core.is_suppressed(conn, email_addr) else "queued"
            cur = conn.execute(
                "INSERT INTO leads(campaign_id, import_id, email, fields_json, status, unsub_token, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (campaign_id, import_id, email_addr, json.dumps(fields), status, secrets.token_urlsafe(24), now))
            if status == "suppressed":
                counts["suppressed"] += 1
                core.log_event(conn, "suppressed_skip", "on suppression list at import", cur.lastrowid,
                               campaign_id, email_addr)
            else:
                counts["accepted"] += 1
        conn.execute("UPDATE imports SET rows=?, accepted=?, suppressed=?, invalid=?, duplicates=? WHERE id=?",
                     (counts["rows"], counts["accepted"], counts["suppressed"], counts["invalid"],
                      counts["duplicates"], import_id))
        flash(f"Imported {upload.filename}: {counts['accepted']} queued, {counts['suppressed']} suppressed, "
              f"{counts['invalid']} invalid, {counts['duplicates']} duplicates.", "ok")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.get("/campaigns/<int:campaign_id>/preview")
    def campaign_preview(campaign_id):
        conn = get_db()
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            abort(404)
        steps = core.campaign_steps(conn, campaign_id)
        mailboxes_ = core.campaign_mailboxes(conn, campaign_id)
        previews = []
        if steps:
            leads = conn.execute(
                "SELECT * FROM leads WHERE campaign_id=? AND status NOT IN ('suppressed') ORDER BY id LIMIT 3",
                (campaign_id,)).fetchall()
            for lead in leads:
                fields = json.loads(lead["fields_json"] or "{}")
                fields.setdefault("email", lead["email"])
                item = {"lead": lead}
                try:
                    item["subject"] = mailer.render(steps[0]["subject"], fields)
                    body = mailer.render(steps[0]["body"], fields)
                    if mailboxes_:
                        business, address = core.resolve_sender(conn, mailboxes_[0])
                        url = f"{config.public_base_url() or '{PUBLIC_BASE_URL}'}/u/{lead['unsub_token']}"
                        sig = mailboxes_[0]["signature"]
                        text = body + (("\n\n" + sig.strip()) if sig and sig.strip() else "")
                        item["text"] = text + "\n\n--\n" + mailer.footer_text(business, address or "(SENDER ADDRESS MISSING)", url)
                    else:
                        item["text"] = body + "\n\n--\n(footer added at send: business name, postal address, unsubscribe link)"
                except mailer.MergeError as exc:
                    item["error"] = str(exc)
                previews.append(item)
        return render_template("preview.html", campaign=campaign, previews=previews, has_steps=bool(steps))

    @app.post("/campaigns/<int:campaign_id>/start")
    def campaign_start(campaign_id):
        conn = get_db()
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            abort(404)
        core.enqueue_suppression_check(conn, campaign_id)
        errors = core.validate_start(conn, campaign_id)
        if errors:
            flash("Cannot start campaign:", "error")
            for e in errors:
                flash(e, "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id)), 303
        conn.execute("UPDATE campaigns SET status='active', paused_reason=NULL WHERE id=?", (campaign_id,))
        flash("Campaign started. The worker sends inside the send window, within caps.", "ok")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.post("/campaigns/<int:campaign_id>/pause")
    def campaign_pause(campaign_id):
        conn = get_db()
        conn.execute("UPDATE campaigns SET status='paused', paused_reason='Paused manually' WHERE id=? AND status='active'",
                     (campaign_id,))
        flash("Campaign paused.", "ok")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.post("/campaigns/<int:campaign_id>/resume")
    def campaign_resume(campaign_id):
        conn = get_db()
        core.enqueue_suppression_check(conn, campaign_id)
        errors = core.validate_start(conn, campaign_id, resume=True)
        if errors:
            flash("Cannot resume campaign:", "error")
            for e in errors:
                flash(e, "error")
            return redirect(url_for("campaign_detail", campaign_id=campaign_id)), 303
        conn.execute("UPDATE campaigns SET status='active', paused_reason=NULL WHERE id=? AND status='paused'",
                     (campaign_id,))
        flash("Campaign resumed.", "ok")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    @app.get("/campaigns/<int:campaign_id>/leads")
    def campaign_leads(campaign_id):
        conn = get_db()
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            abort(404)
        status = request.args.get("status", "")
        sql = ("SELECT l.*, m.label AS mailbox_label, m.from_email AS mailbox_email FROM leads l "
               "LEFT JOIN mailboxes m ON m.id=l.mailbox_id WHERE l.campaign_id=?")
        args = [campaign_id]
        if status == "waiting_cap":
            sql += " AND l.wait_reason='waiting on cap' AND l.status IN ('queued','active')"
        elif status:
            sql += " AND l.status=?"
            args.append(status)
        leads = conn.execute(sql + " ORDER BY l.id", args).fetchall()
        return render_template("leads.html", campaign=campaign, leads=leads, status=status,
                               statuses=core.LEAD_STATUSES, stats=core.campaign_stats(conn, campaign_id))

    @app.get("/leads/<int:lead_id>")
    def lead_detail(lead_id):
        conn = get_db()
        lead = conn.execute(
            "SELECT l.*, m.label AS mailbox_label, m.from_email AS mailbox_email FROM leads l "
            "LEFT JOIN mailboxes m ON m.id=l.mailbox_id WHERE l.id=?", (lead_id,)).fetchone()
        if lead is None:
            abort(404)
        sends = conn.execute(
            "SELECT s.*, m.from_email AS mailbox_email FROM sends s LEFT JOIN mailboxes m ON m.id=s.mailbox_id "
            "WHERE s.lead_id=? ORDER BY s.id", (lead_id,)).fetchall()
        events = conn.execute("SELECT * FROM events WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (lead["campaign_id"],)).fetchone()
        fields = json.loads(lead["fields_json"] or "{}")
        unsub_url = f"{config.public_base_url()}/u/{lead['unsub_token']}"
        return render_template("lead.html", lead=lead, sends=sends, events=events, campaign=campaign,
                               fields=fields, unsub_url=unsub_url)

    # ------------------------------------------------------------ suppression
    @app.route("/suppression", methods=["GET", "POST"])
    def suppression():
        conn = get_db()
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                value = (request.form.get("value") or "").strip().lower()
                note = (request.form.get("note") or "").strip()[:200]
                if "@" in value and not value.startswith("@"):
                    if not core.valid_email(value):
                        flash("Not a valid email address.", "error")
                    elif core.add_suppression(conn, email=value, source="manual", note=note):
                        flash(f"Suppressed {value}.", "ok")
                    else:
                        flash(f"{value} is already suppressed.", "ok")
                else:
                    domain = value.lstrip("@")
                    if not core.DOMAIN_RE.match(domain):
                        flash("Enter an email address or a domain like example.com.", "error")
                    elif core.add_suppression(conn, domain=domain, source="manual", note=note):
                        flash(f"Suppressed every address at {domain}.", "ok")
                    else:
                        flash(f"{domain} is already suppressed.", "ok")
            elif action == "remove":
                conn.execute("DELETE FROM suppression WHERE id=?", (request.form.get("id"),))
                flash("Removed from suppression. Lead statuses are not changed automatically.", "ok")
            elif action == "import":
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    flash("Choose a CSV file.", "error")
                else:
                    added = skipped = 0
                    reader = csv.reader(io.StringIO(_read_upload(upload)))
                    header = None
                    for row in reader:
                        if not row or not any(c.strip() for c in row):
                            continue
                        cells = [c.strip().lower() for c in row]
                        if header is None and any(c in ("email", "domain") for c in cells):
                            header = cells
                            continue
                        record = dict(zip(header, cells)) if header else {}
                        email_v = record.get("email") if header else (cells[0] if "@" in cells[0] else "")
                        domain_v = record.get("domain") if header else ("" if "@" in cells[0] else cells[0])
                        if email_v and core.valid_email(email_v):
                            ok = core.add_suppression(conn, email=email_v, source="csv_import", note=upload.filename[:100])
                        elif domain_v and core.DOMAIN_RE.match(domain_v.lstrip("@")):
                            ok = core.add_suppression(conn, domain=domain_v, source="csv_import", note=upload.filename[:100])
                        else:
                            ok = False
                        added, skipped = (added + 1, skipped) if ok else (added, skipped + 1)
                    flash(f"Suppression import: {added} added, {skipped} skipped (invalid or already present).", "ok")
            return redirect(url_for("suppression"))
        q = (request.args.get("q") or "").strip().lower()
        sql, args = "SELECT * FROM suppression", []
        if q:
            sql += " WHERE email LIKE ? OR domain LIKE ?"
            args = [f"%{q}%", f"%{q}%"]
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT 1000", args).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
        return render_template("suppression.html", rows=rows, total=total, q=q)

    # ------------------------------------------------------------ exports
    @app.get("/export/campaign/<int:campaign_id>/leads.csv")
    def export_leads(campaign_id):
        conn = get_db()
        leads = conn.execute(
            "SELECT l.*, m.from_email AS mailbox_email FROM leads l LEFT JOIN mailboxes m ON m.id=l.mailbox_id "
            "WHERE l.campaign_id=? ORDER BY l.id", (campaign_id,)).fetchall()
        keys = []
        for lead in leads:
            for k in json.loads(lead["fields_json"] or "{}"):
                if k not in keys:
                    keys.append(k)
        rows = []
        for lead in leads:
            f = json.loads(lead["fields_json"] or "{}")
            rows.append([lead["email"], lead["status"], lead["current_step"], lead["mailbox_email"] or "",
                         lead["last_sent_at"] or "", lead["wait_reason"] or "", lead["reply_snippet"] or ""]
                        + [f.get(k, "") for k in keys])
        return _csv_response(rows, ["email", "status", "current_step", "mailbox", "last_sent_at_utc", "wait_reason",
                                    "reply_snippet"] + keys, f"campaign-{campaign_id}-leads.csv")

    @app.get("/export/suppression.csv")
    def export_suppression():
        rows = [[r["email"] or "", r["domain"] or "", r["source"], r["note"] or "", r["created_at"]]
                for r in get_db().execute("SELECT * FROM suppression ORDER BY id")]
        return _csv_response(rows, ["email", "domain", "source", "note", "created_at_utc"], "suppression.csv")

    @app.get("/export/sends.csv")
    def export_sends():
        rows = [[r["id"], r["campaign_id"], r["campaign_name"] or "", r["recipient"], r["step_pos"],
                 r["mailbox_email"] or "", r["message_id"] or "", r["sent_at"], r["status"], r["smtp_result"] or "",
                 r["attempt"]]
                for r in get_db().execute(
                    "SELECT s.*, c.name AS campaign_name, m.from_email AS mailbox_email FROM sends s "
                    "LEFT JOIN campaigns c ON c.id=s.campaign_id LEFT JOIN mailboxes m ON m.id=s.mailbox_id ORDER BY s.id")]
        return _csv_response(rows, ["id", "campaign_id", "campaign", "recipient", "step", "mailbox", "message_id",
                                    "sent_at_utc", "status", "smtp_result", "attempt"], "sends.csv")

    # ------------------------------------------------------------ settings
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        conn = get_db()
        if request.method == "POST":
            if request.form.get("action") == "reset":
                db.delete_setting(conn, "sender_business_name")
                db.delete_setting(conn, "sender_address")
                flash("Settings cleared. Sender name and address now come from .env.", "ok")
            else:
                db.set_setting(conn, "sender_business_name", (request.form.get("sender_business_name") or "").strip())
                db.set_setting(conn, "sender_address",
                               (request.form.get("sender_address") or "").replace("\r\n", "\n").strip())
                flash("Settings saved. These values override .env (a blank value here counts as empty).", "ok")
            return redirect(url_for("settings"))
        overrides = {k: db.get_setting(conn, k) for k in ("sender_business_name", "sender_address")}
        defaults = {
            "DEFAULT_DAILY_CAP": config.default_daily_cap(), "hard max": config.HARD_MAX_DAILY_CAP,
            "RAMP_START": config.ramp_start(), "RAMP_STEP": config.ramp_step(),
            "MIN_GAP_SECONDS": config.min_gap(), "MAX_GAP_SECONDS": config.max_gap(),
            "RECIPIENT_DOMAIN_PER_HOUR": config.domain_per_hour(), "MAX_BOUNCE_RATE": config.max_bounce_rate(),
            "WORKER_TICK_SECONDS": config.tick_seconds(), "IMAP_POLL_SECONDS": config.imap_poll_seconds(),
            "PUBLIC_BASE_URL": config.public_base_url() or "(not set: campaigns cannot start)",
        }
        return render_template(
            "settings.html",
            business=core.sender_business_name(conn), address=core.global_sender_address(conn),
            overrides=overrides, env_business=config.env("SENDER_BUSINESS_NAME", ""),
            env_address=config.env("SENDER_ADDRESS", ""), defaults=defaults)

    return app
