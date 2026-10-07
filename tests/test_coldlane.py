"""ColdLane tests. Real SMTP via an aiosmtpd stand-in; IMAP via an in-memory fake.

Run: pip install -r requirements-dev.txt && python -m unittest discover -s tests -v
"""
import datetime as dt
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from email import message_from_bytes, policy

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiosmtpd.controller import Controller  # noqa: E402

from coldlane import core, crypto, db, engine, mailer  # noqa: E402
from coldlane.scripts.inject_bounce import build_dsn  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample-leads.csv")


class Handler:
    def __init__(self):
        self.messages = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address.startswith("bounce"):
            return "550 5.1.1 No such user"
        if address.startswith("tempfail"):
            return "451 4.3.0 Try again later"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append({"from": envelope.mail_from, "to": list(envelope.rcpt_tos), "raw": envelope.content})
        return "250 Message accepted"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeIMAP:
    """Minimal imaplib stand-in. Records whether anything tried to write."""

    def __init__(self, messages):
        self.messages = messages  # list of (uid, raw bytes)
        self.untagged_responses = {"UIDVALIDITY": [b"1"]}
        self.readonly = None
        self.writes = []

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = " ".join(a for a in args if a)
            if criteria.startswith("UID "):
                low = int(criteria.split()[1].split(":")[0])
                uids = [u for u, _ in self.messages if u >= low] or [self.messages[-1][0]] if self.messages else []
            else:
                uids = [u for u, _ in self.messages]
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if command == "FETCH":
            for u, raw in self.messages:
                if str(u) == str(args[0]):
                    assert "PEEK" in args[1]
                    return "OK", [(f"{u} (BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", [None]
        self.writes.append(command)
        return "NO", [b"unsupported"]

    def logout(self):
        return "BYE", [b""]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = Handler()
        cls.port = free_port()
        cls.controller = Controller(cls.handler, hostname="127.0.0.1", port=cls.port)
        cls.controller.start()

    @classmethod
    def tearDownClass(cls):
        cls.controller.stop()

    def setUp(self):
        self.handler.messages.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env_backup = dict(os.environ)
        os.environ.update({
            "DATABASE_PATH": os.path.join(self.tmp.name, "coldlane.db"),
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "SECRET_KEY": "test-secret",
            "OWNER_PASSWORD": "testpass",
            "PUBLIC_BASE_URL": "http://127.0.0.1:8080",
            "SENDER_BUSINESS_NAME": "Harbor Growth Agency",
            "SENDER_ADDRESS": "12 Test Street, Testville, CA 90000, USA",
            "MIN_GAP_SECONDS": "0",
            "MAX_GAP_SECONDS": "0",
            "RECIPIENT_DOMAIN_PER_HOUR": "100",
            "WORKER_TICK_SECONDS": "5",
            "IMAP_POLL_SECONDS": "10",
        })
        os.environ.pop("DEFAULT_DAILY_CAP", None)
        crypto.reset_cache()
        from coldlane.web import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.conn = db.connect()
        self.now = dt.datetime(2026, 10, 7, 15, 0, tzinfo=dt.timezone.utc)  # Wednesday

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.env_backup)
        crypto.reset_cache()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def login(self):
        r = self.client.post("/login", data={"password": "testpass"})
        self.assertEqual(r.status_code, 302)

    def add_mailbox(self, email_addr, cap=3, ramp=False, port=None, password="secret-pw-123"):
        data = {"label": email_addr, "from_name": "Sam Sender", "from_email": email_addr,
                "smtp_host": "127.0.0.1", "smtp_port": str(port or self.port), "smtp_security": "none",
                "smtp_user": email_addr, "smtp_password": password, "imap_host": "127.0.0.1",
                "imap_port": "3143", "daily_cap": str(cap), "active": "1"}
        if ramp:
            data["ramp_enabled"] = "1"
        r = self.client.post("/mailboxes/new", data=data)
        self.assertEqual(r.status_code, 302, r.data)
        return int(r.headers["Location"].rstrip("/").split("/")[-1])

    def add_campaign(self, mailbox_ids, steps=None, start=0, end=24, days="0123456", name="Test"):
        steps = steps or [("Quick question for {{company}}", "Hi {{first_name|there}},\n\nAre you the right person at {{company}}?", 0, "minutes"),
                          ("", "Just bumping this, {{first_name|there}}.", 1, "minutes")]
        data = {"name": name, "timezone": "UTC", "window_start_hour": str(start), "window_end_hour": str(end)}
        form = [(k, v) for k, v in data.items()]
        form += [("window_days", d) for d in days]
        form += [("mailboxes", str(m)) for m in mailbox_ids]
        for i, (subj, body, delay, unit) in enumerate(steps, 1):
            form += [(f"subject_{i}", subj), (f"body_{i}", body), (f"delay_{i}", str(delay)), (f"delay_unit_{i}", unit)]
        from werkzeug.datastructures import MultiDict
        r = self.client.post("/campaigns/new", data=MultiDict(form))
        self.assertEqual(r.status_code, 302, r.data)
        return int(r.headers["Location"].rstrip("/").split("/")[-1])

    def import_csv(self, cid, content=None, lawful=True, filename="sample-leads.csv"):
        if content is None:
            with open(SAMPLE, "rb") as fh:
                content = fh.read()
        data = {"file": (io.BytesIO(content), filename), "acknowledged_by": "tester"}
        if lawful:
            data["lawful_basis"] = "1"
        return self.client.post(f"/campaigns/{cid}/import", data=data, content_type="multipart/form-data")

    def start(self, cid):
        return self.client.post(f"/campaigns/{cid}/start", follow_redirects=True)

    def lead(self, cid, email_addr):
        return self.conn.execute("SELECT * FROM leads WHERE campaign_id=? AND email=?", (cid, email_addr)).fetchone()

    def sent_to(self, addr):
        return [m for m in self.handler.messages if addr in m["to"]]

    def setup_running(self, cap=3):
        self.login()
        a = self.add_mailbox("a@agency.test", cap=cap)
        b = self.add_mailbox("b@agency.test", cap=cap)
        cid = self.add_campaign([a, b])
        self.import_csv(cid)
        r = self.start(cid)
        self.assertIn(b"Campaign started", r.data)
        return a, b, cid


class TestAuthHealthSecurity(Base):
    def test_auth_gates(self):
        for path in ["/", "/mailboxes", "/mailboxes/new", "/campaigns/new", "/suppression", "/settings",
                     "/export/sends.csv", "/export/suppression.csv"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.post("/login", data={"password": "wrong"}).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_health_shape(self):
        body = self.client.get("/health").get_json()
        self.assertEqual(set(body), {"status", "worker_last_tick", "worker_ok", "mailboxes_active"})
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["worker_ok"])
        engine.tick(self.conn)
        body = self.client.get("/health").get_json()
        self.assertTrue(body["worker_ok"])
        self.assertIsNotNone(body["worker_last_tick"])
        db.set_state(self.conn, "last_tick", core.ts(core.utcnow() - dt.timedelta(seconds=16)))
        self.assertFalse(self.client.get("/health").get_json()["worker_ok"])

    def test_password_encrypted_at_rest(self):
        self.login()
        mid = self.add_mailbox("a@agency.test")
        for suffix in ("", "-wal"):
            path = os.environ["DATABASE_PATH"] + suffix
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    self.assertNotIn(b"secret-pw-123", fh.read())
        row = self.conn.execute("SELECT * FROM mailboxes WHERE id=?", (mid,)).fetchone()
        self.assertNotIn("secret-pw-123", row["smtp_pass_enc"])
        self.assertEqual(crypto.decrypt(row["smtp_pass_enc"]), "secret-pw-123")
        self.assertEqual(crypto.decrypt(row["imap_pass_enc"]), "secret-pw-123")
        page = self.client.get(f"/mailboxes/{mid}").data
        self.assertNotIn(b"secret-pw-123", page)

    def test_cap_150_rejected_and_defaults(self):
        self.login()
        page = self.client.get("/mailboxes/new").data.decode()
        self.assertIn('name="daily_cap" type="number" min="1" max="100" value="30"', page)
        self.assertRegex(page, r'name="ramp_enabled" value="1"\s+checked')
        r = self.client.post("/mailboxes/new", data={"from_email": "x@agency.test", "smtp_host": "h", "daily_cap": "150"})
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Daily cap must be between 1 and 100", r.data)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0], 0)
        mid = self.add_mailbox("a@agency.test", cap=30, ramp=True)
        mb = self.conn.execute("SELECT * FROM mailboxes WHERE id=?", (mid,)).fetchone()
        self.assertEqual(core.effective_cap(mb, self.now), 10)  # ramp day 0 = RAMP_START
        self.conn.execute("UPDATE mailboxes SET first_send_date=? WHERE id=?",
                          ((core.local_date(self.now) - dt.timedelta(days=3)).isoformat(), mid))
        mb = self.conn.execute("SELECT * FROM mailboxes WHERE id=?", (mid,)).fetchone()
        self.assertEqual(core.effective_cap(mb, self.now), 25)
        # edit to 150 also rejected
        r = self.client.post(f"/mailboxes/{mid}", data={"from_email": "a@agency.test", "smtp_host": "h", "daily_cap": "150"})
        self.assertEqual(r.status_code, 400)

    def test_settings_tracking_statement_and_no_tracking_ui(self):
        self.login()
        page = self.client.get("/settings").data.decode()
        self.assertIn("Open/click tracking: off. ColdLane does not track opens or clicks.", page)
        for path in ["/", "/mailboxes", "/mailboxes/new", "/campaigns/new", "/suppression"]:
            text = self.client.get(path).data.decode().lower()
            for word in ["warmup", "warm-up", "scrap", "enrich", "pixel"]:
                self.assertNotIn(word, text, (path, word))


class TestImportAndStart(Base):
    def test_lawful_basis_required(self):
        self.login()
        a = self.add_mailbox("a@agency.test")
        cid = self.add_campaign([a])
        r = self.import_csv(cid, lawful=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
        r = self.import_csv(cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads WHERE status='queued'").fetchone()[0], 10)
        imp = self.conn.execute("SELECT * FROM imports").fetchone()
        self.assertEqual((imp["acknowledged_by"], imp["filename"]), ("tester", "sample-leads.csv"))
        self.assertTrue(imp["acknowledged_at"])
        # dedupe + lowercase + invalid
        self.import_csv(cid, b"email,first_name\nLEAD1@Example.test,A\nnot-an-email,B\nnew@example.test,C\nnew@example.test,D\n")
        imp = self.conn.execute("SELECT * FROM imports ORDER BY id DESC").fetchone()
        self.assertEqual((imp["accepted"], imp["invalid"], imp["duplicates"]), (1, 1, 2))

    def test_suppressed_import_never_sent(self):
        self.login()
        self.client.post("/suppression", data={"action": "add", "value": "lead4@example.test"})
        a, b, cid = None, None, None
        a = self.add_mailbox("a@agency.test", cap=20)
        cid = self.add_campaign([a])
        self.import_csv(cid)
        self.assertEqual(self.lead(cid, "lead4@example.test")["status"], "suppressed")
        self.start(cid)
        for i in range(4):
            engine.send_pass(self.conn, self.now + dt.timedelta(minutes=2 * i))
        self.assertEqual(self.sent_to("lead4@example.test"), [])
        self.assertGreater(len(self.handler.messages), 0)

    def test_start_blocked_without_sender_address_or_base_url(self):
        self.login()
        a = self.add_mailbox("a@agency.test")
        cid = self.add_campaign([a])
        self.import_csv(cid)
        self.client.post("/settings", data={"action": "save", "sender_business_name": "Harbor Growth Agency", "sender_address": ""})
        r = self.start(cid)
        self.assertIn(b"Cannot start campaign", r.data)
        self.assertIn(b"Sender postal address is empty", r.data)
        self.assertEqual(self.conn.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()[0], "draft")
        self.client.post("/settings", data={"action": "reset"})
        os.environ["PUBLIC_BASE_URL"] = ""
        r = self.start(cid)
        self.assertIn(b"PUBLIC_BASE_URL is not set", r.data)
        os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8080"
        r = self.start(cid)
        self.assertIn(b"Campaign started", r.data)

    def test_start_blocked_without_leads_steps_mailbox(self):
        self.login()
        cid = self.add_campaign([], steps=[("S", "B", 0, "minutes")])
        r = self.start(cid)
        self.assertIn(b"active (not paused) mailbox", r.data)
        self.assertIn(b"Import at least 1 lead", r.data)


class TestSending(Base):
    def test_rotation_alternates_and_cap(self):
        a, b, cid = self.setup_running(cap=3)
        engine.send_pass(self.conn, self.now)
        froms = [m["from"] for m in self.handler.messages]
        self.assertEqual(froms, ["a@agency.test", "b@agency.test"] * 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads WHERE campaign_id=? AND wait_reason='waiting on cap'", (cid,)).fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='cap_skip'").fetchone()[0], 4)
        # later ticks the same day: still capped, step 2 not sent, cap_skip not duplicated
        engine.send_pass(self.conn, self.now + dt.timedelta(minutes=5))
        self.assertEqual(len(self.handler.messages), 6)
        # 4 new leads + 6 due follow-ups now wait on cap (one cap_skip per lead per day)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='cap_skip'").fetchone()[0], 4 + 6)
        page = self.client.get(f"/campaigns/{cid}/leads?status=waiting_cap").data
        self.assertIn(b"waiting on cap", page)

    def test_gap_spacing(self):
        os.environ["MIN_GAP_SECONDS"] = "90"
        os.environ["MAX_GAP_SECONDS"] = "90"
        a, b, cid = self.setup_running(cap=3)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(len(self.handler.messages), 2)  # one per mailbox, then gap
        engine.send_pass(self.conn, self.now + dt.timedelta(seconds=30))
        self.assertEqual(len(self.handler.messages), 2)
        engine.send_pass(self.conn, self.now + dt.timedelta(seconds=91))
        self.assertEqual(len(self.handler.messages), 4)

    def test_domain_throttle(self):
        os.environ["RECIPIENT_DOMAIN_PER_HOUR"] = "2"
        a, b, cid = self.setup_running(cap=3)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(len(self.handler.messages), 2)
        self.assertIn("domain throttle", self.lead(cid, "lead3@example.test")["wait_reason"])

    def test_window_excluding_now_sends_nothing(self):
        self.login()
        a = self.add_mailbox("a@agency.test")
        cid = self.add_campaign([a], start=1, end=2)  # 01:00-02:00 UTC, now is 15:00
        self.import_csv(cid)
        self.start(cid)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(self.handler.messages, [])
        cid2 = self.add_campaign([a], days="56", name="weekend")  # Sat/Sun only; now is Wednesday
        self.import_csv(cid2, b"email\nother@example.test\n")
        self.start(cid2)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(self.handler.messages, [])

    def test_headers_footer_merge_no_pixel(self):
        a, b, cid = self.setup_running()
        engine.send_pass(self.conn, self.now)
        raw = self.sent_to("lead1@example.test")[0]["raw"]
        msg = message_from_bytes(raw, policy=policy.default)
        lead = self.lead(cid, "lead1@example.test")
        url = f"http://127.0.0.1:8080/u/{lead['unsub_token']}"
        self.assertIn(f"<{url}>", msg["List-Unsubscribe"])
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(msg["Subject"], "Quick question for Brightline Dental")
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        text = msg.get_body(("plain",)).get_content()
        html = msg.get_body(("html",)).get_content()
        self.assertIn("Hi Ava,", text)
        self.assertIn("Harbor Growth Agency", text)
        self.assertIn("12 Test Street, Testville, CA 90000, USA", text)
        self.assertIn(f"Don't want these emails? Unsubscribe: {url}", text)
        self.assertIn("12 Test Street", html)
        self.assertIn(url, html)
        for body in (text, html, raw.decode(errors="replace")):
            self.assertNotIn("{{", body)
            self.assertNotIn("}}", body)
        self.assertNotIn("<img", html.lower())
        self.assertEqual(re.findall(r'href="([^"]+)"', html), [url])  # only the unsubscribe link

    def test_merge_error_skips_lead(self):
        self.login()
        a = self.add_mailbox("a@agency.test", cap=20)
        cid = self.add_campaign([a], steps=[("Hi {{first_name}}", "About {{company}}", 0, "minutes")])
        self.import_csv(cid, b"email,first_name,company\nok@example.test,Ok,Acme\nnofirst@example.test,,Beta\n")
        self.start(cid)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(self.lead(cid, "nofirst@example.test")["status"], "merge_error")
        self.assertEqual(self.sent_to("nofirst@example.test"), [])
        self.assertEqual(len(self.sent_to("ok@example.test")), 1)
        page = self.client.get(f"/campaigns/{cid}/preview").data
        self.assertIn(b"merge_error", page)
        self.assertIn(b"Hi Ok", page)

    def test_step2_same_mailbox_in_reply_to(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        first = {m["to"][0]: m for m in self.handler.messages}
        engine.send_pass(self.conn, self.now + dt.timedelta(seconds=30))  # step 2 not due yet
        self.assertEqual(len(self.handler.messages), 10)
        engine.send_pass(self.conn, self.now + dt.timedelta(minutes=2))
        follow = [m for m in self.handler.messages[10:] if m["to"][0] == "lead1@example.test"]
        self.assertEqual(len(follow), 1)
        m1 = message_from_bytes(first["lead1@example.test"]["raw"], policy=policy.default)
        m2 = message_from_bytes(follow[0]["raw"], policy=policy.default)
        self.assertEqual(follow[0]["from"], first["lead1@example.test"]["from"])
        self.assertEqual(m2["In-Reply-To"], m1["Message-ID"])
        self.assertIn(m1["Message-ID"], m2["References"])
        self.assertEqual(m2["Subject"], "Re: Quick question for Brightline Dental")
        self.assertIn("List-Unsubscribe", m2)
        self.assertEqual(self.lead(cid, "lead1@example.test")["status"], "completed")

    def test_smtp_hard_bounce_and_soft_retry(self):
        self.login()
        a = self.add_mailbox("a@agency.test", cap=20)
        cid = self.add_campaign([a], steps=[("Hello", "Body", 0, "minutes")])
        self.import_csv(cid, b"email\nbounce1@example.test\ntempfail1@example.test\nfine@example.test\n")
        self.start(cid)
        engine.send_pass(self.conn, self.now)
        self.assertEqual(self.lead(cid, "bounce1@example.test")["status"], "bounced")
        self.assertTrue(core.is_suppressed(self.conn, "bounce1@example.test"))
        self.assertEqual(self.lead(cid, "tempfail1@example.test")["attempts"], 1)
        for i in range(1, 4):
            engine.send_pass(self.conn, self.now + dt.timedelta(minutes=10 * i))
        self.assertEqual(self.lead(cid, "tempfail1@example.test")["status"], "error")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sends WHERE recipient='tempfail1@example.test'").fetchone()[0], 3)

    def test_mailbox_auto_pause_after_3_failures(self):
        self.login()
        dead = self.add_mailbox("dead@agency.test", cap=20, port=free_port())
        cid = self.add_campaign([dead], steps=[("Hello", "Body", 0, "minutes")])
        self.import_csv(cid)
        self.start(cid)
        for i in range(3):
            engine.send_pass(self.conn, self.now + dt.timedelta(minutes=i))
        mb = self.conn.execute("SELECT * FROM mailboxes WHERE id=?", (dead,)).fetchone()
        self.assertIn("Auto-paused after 3 consecutive SMTP failures", mb["paused_reason"])
        self.assertEqual(self.lead(cid, "lead1@example.test")["status"], "queued")
        ok, results = mailer.test_connection(mb)
        self.assertFalse(ok)
        self.assertIn("SMTP FAILED", results[0])

    def test_bounce_rate_auto_pause(self):
        self.login()
        a = self.add_mailbox("a@agency.test", cap=100)
        cid = self.add_campaign([a], steps=[("Hello", "Body", 0, "minutes")])
        rows = "\n".join([f"ok{i}@example.test" for i in range(18)] + ["bounce1@example.test", "bounce2@example.test", "ok99@example.test"])
        self.import_csv(cid, ("email\n" + rows + "\n").encode())
        self.start(cid)
        engine.send_pass(self.conn, self.now)
        c = self.conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        self.assertEqual(c["status"], "paused")
        self.assertIn("bounce rate", c["paused_reason"])


class TestInbound(Base):
    def reply_raw(self, frm, to, in_reply_to="", auto=False, body="Sounds good, let's talk Tuesday.", subject="Re: hi"):
        headers = f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: <r{abs(hash(body + frm))}@example.test>\r\n"
        if in_reply_to:
            headers += f"In-Reply-To: {in_reply_to}\r\nReferences: {in_reply_to}\r\n"
        if auto:
            headers += "Auto-Submitted: auto-replied\r\n"
        return (headers + "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body + "\r\n").encode()

    def test_reply_stops_across_campaigns(self):
        a, b, cid = self.setup_running(cap=10)
        cid2 = self.add_campaign([a, b], name="second")
        self.import_csv(cid2, b"email,first_name,company\nlead1@example.test,Ava,Brightline\n")
        self.start(cid2)
        engine.send_pass(self.conn, self.now)
        mid = self.lead(cid, "lead1@example.test")["last_message_id"]
        result = engine.process_incoming(self.conn, self.reply_raw("lead1@example.test", "a@agency.test", mid))
        self.assertEqual(result, "reply")
        l1, l2 = self.lead(cid, "lead1@example.test"), self.lead(cid2, "lead1@example.test")
        self.assertEqual((l1["status"], l2["status"]), ("replied", "replied"))
        self.assertIn("Sounds good", l1["reply_snippet"])
        before = len(self.sent_to("lead1@example.test"))
        engine.send_pass(self.conn, self.now + dt.timedelta(minutes=5))
        self.assertEqual(len(self.sent_to("lead1@example.test")), before)

    def test_reply_by_from_address_only(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        engine.process_incoming(self.conn, self.reply_raw("Lead2@Example.test", "b@agency.test"))
        self.assertEqual(self.lead(cid, "lead2@example.test")["status"], "replied")

    def test_auto_reply_does_not_stop(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        mid = self.lead(cid, "lead2@example.test")["last_message_id"]
        result = engine.process_incoming(self.conn, self.reply_raw("lead2@example.test", "b@agency.test", mid, auto=True,
                                                                   body="I'm out of office"))
        self.assertEqual(result, "auto_reply")
        self.assertEqual(self.lead(cid, "lead2@example.test")["status"], "active")
        engine.send_pass(self.conn, self.now + dt.timedelta(minutes=2))
        self.assertEqual(len(self.sent_to("lead2@example.test")), 2)
        ev = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='auto_reply'").fetchone()[0]
        self.assertEqual(ev, 1)

    def test_dsn_511_bounces_and_suppresses(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        result = engine.process_incoming(self.conn, build_dsn("a@agency.test", "lead3@example.test", "5.1.1"))
        self.assertEqual(result, "bounce_hard:lead3@example.test")
        self.assertEqual(self.lead(cid, "lead3@example.test")["status"], "bounced")
        self.assertTrue(core.is_suppressed(self.conn, "lead3@example.test"))
        src = self.conn.execute("SELECT source FROM suppression WHERE email='lead3@example.test'").fetchone()[0]
        self.assertEqual(src, "bounce")
        # 4.x.x is logged only
        result = engine.process_incoming(self.conn, build_dsn("a@agency.test", "lead4@example.test", "4.2.2"))
        self.assertEqual(result, "bounce_soft:lead4@example.test")
        self.assertEqual(self.lead(cid, "lead4@example.test")["status"], "active")

    def test_optout_reply_unsubscribes(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        engine.process_incoming(self.conn, self.reply_raw("lead5@example.test", "a@agency.test", body="Remove me"))
        self.assertEqual(self.lead(cid, "lead5@example.test")["status"], "unsubscribed")
        self.assertEqual(self.conn.execute("SELECT source FROM suppression WHERE email='lead5@example.test'").fetchone()[0], "reply_optout")

    def test_imap_poll_readonly_and_uid_tracking(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        mid = self.lead(cid, "lead1@example.test")["last_message_id"]
        fake = FakeIMAP([(5, self.reply_raw("lead1@example.test", "a@agency.test", mid))])
        orig = mailer.imap_open
        mailer.imap_open = lambda mb, timeout=30: fake if mb["id"] == a else FakeIMAP([])
        try:
            self.assertEqual(engine.imap_pass(self.conn, force=True), 1)
            self.assertTrue(fake.readonly)
            self.assertEqual(fake.writes, [])
            self.assertEqual(self.lead(cid, "lead1@example.test")["status"], "replied")
            self.assertEqual(self.conn.execute("SELECT last_imap_uid FROM mailboxes WHERE id=?", (a,)).fetchone()[0], 5)
            self.assertEqual(engine.imap_pass(self.conn, force=True), 0)  # nothing new since UID 5
        finally:
            mailer.imap_open = orig


class TestUnsubscribe(Base):
    def test_post_one_click_and_second_campaign(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        token = self.lead(cid, "lead6@example.test")["unsub_token"]
        self.client.get("/logout")
        r = self.client.post(f"/u/{token}", data="List-Unsubscribe=One-Click",
                             content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.lead(cid, "lead6@example.test")["status"], "unsubscribed")
        self.assertTrue(core.is_suppressed(self.conn, "lead6@example.test"))
        self.login()
        cid2 = self.add_campaign([a, b], name="second")
        self.import_csv(cid2, b"email,first_name,company\nlead6@example.test,Farah,Greenleaf\nfresh@example.test,F,Co\n")
        self.assertEqual(self.lead(cid2, "lead6@example.test")["status"], "suppressed")
        self.start(cid2)
        before = len(self.sent_to("lead6@example.test"))
        engine.send_pass(self.conn, self.now + dt.timedelta(minutes=5))
        self.assertEqual(len(self.sent_to("lead6@example.test")), before)

    def test_get_unsubscribes_and_shows_confirmation_and_undo(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        token = self.lead(cid, "lead7@example.test")["unsub_token"]
        self.client.get("/logout")
        r = self.client.get(f"/u/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"You're unsubscribed", r.data)
        self.assertEqual(self.lead(cid, "lead7@example.test")["status"], "unsubscribed")
        self.assertTrue(core.is_suppressed(self.conn, "lead7@example.test"))
        r = self.client.post(f"/u/{token}/undo")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(core.is_suppressed(self.conn, "lead7@example.test"))
        self.assertEqual(self.lead(cid, "lead7@example.test")["status"], "active")
        self.assertEqual(self.client.post("/u/not-a-real-token").status_code, 404)

    def test_suppression_after_queue_checked_at_send(self):
        a, b, cid = self.setup_running(cap=10)
        self.client.post("/suppression", data={"action": "add", "value": "example.test"})  # whole domain
        engine.send_pass(self.conn, self.now)
        self.assertEqual(self.handler.messages, [])
        self.assertEqual(self.lead(cid, "lead1@example.test")["status"], "suppressed")


class TestDashboardExports(Base):
    def test_counts_and_exports(self):
        a, b, cid = self.setup_running(cap=10)
        engine.send_pass(self.conn, self.now)
        mid = self.lead(cid, "lead1@example.test")["last_message_id"]
        engine.process_incoming(self.conn, TestInbound.reply_raw(self, "lead1@example.test", "a@agency.test", mid))
        engine.process_incoming(self.conn, build_dsn("a@agency.test", "lead3@example.test"))
        self.client.post(f"/u/{self.lead(cid, 'lead6@example.test')['unsub_token']}")
        stats = core.campaign_stats(self.conn, cid)
        self.assertEqual((stats["sent"], stats["replied"], stats["bounced"], stats["unsubscribed"]), (10, 1, 1, 1))
        ev = {k: self.conn.execute("SELECT COUNT(*) FROM events WHERE campaign_id=? AND kind=?", (cid, k)).fetchone()[0]
              for k in ("reply", "bounce_hard", "unsubscribe")}
        self.assertEqual(ev, {"reply": 1, "bounce_hard": 1, "unsubscribe": 1})
        page = self.client.get("/").data.decode()
        self.assertIn("<td>10</td><td>1</td><td>1</td><td>1</td>", page)
        for path in [f"/export/campaign/{cid}/leads.csv", "/export/suppression.csv", "/export/sends.csv"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertGreater(len(r.data.decode().strip().splitlines()), 1, path)
        self.assertEqual(self.client.get(f"/campaigns/{cid}").status_code, 200)
        self.assertEqual(self.client.get(f"/leads/{self.lead(cid, 'lead1@example.test')['id']}").status_code, 200)
        self.assertEqual(self.client.get("/mailboxes").status_code, 200)

    def test_suppression_csv_import_export(self):
        self.login()
        data = {"action": "import", "file": (io.BytesIO(b"email,domain\njane@x.test,\n,blocked.test\nbad,\n"), "s.csv")}
        self.client.post("/suppression", data=data, content_type="multipart/form-data")
        rows = self.conn.execute("SELECT email, domain, source FROM suppression ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("jane@x.test", None, "csv_import"), (None, "blocked.test", "csv_import")])
        self.assertTrue(core.is_suppressed(self.conn, "anyone@blocked.test"))
        rid = self.conn.execute("SELECT id FROM suppression WHERE email='jane@x.test'").fetchone()[0]
        self.client.post("/suppression", data={"action": "remove", "id": str(rid)})
        self.assertFalse(core.is_suppressed(self.conn, "jane@x.test"))


class TestKeyFallback(unittest.TestCase):
    def test_generated_key_file_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = dict(os.environ)
            try:
                os.environ["DATABASE_PATH"] = os.path.join(tmp, "coldlane.db")
                os.environ.pop("ENCRYPTION_KEY", None)
                crypto.reset_cache()
                token = crypto.encrypt("pw")
                self.assertTrue(crypto.using_generated_key())
                self.assertTrue(os.path.exists(os.path.join(tmp, crypto.KEY_FILENAME)))
                crypto.reset_cache()
                self.assertEqual(crypto.decrypt(token), "pw")  # same key reloaded from file
                os.environ["ENCRYPTION_KEY"] = "not-a-fernet-key"
                crypto.reset_cache()
                with self.assertRaises(RuntimeError):
                    crypto.get_key()
            finally:
                os.environ.clear()
                os.environ.update(backup)
                crypto.reset_cache()


class TestMerge(unittest.TestCase):
    def test_render(self):
        f = {"first_name": "Ava", "company": ""}
        self.assertEqual(mailer.render("Hi {{first_name}} at {{ company | your team }}", f), "Hi Ava at your team")
        self.assertEqual(mailer.render("Hi {{First Name|there}}", {}), "Hi there")
        with self.assertRaises(mailer.MergeError):
            mailer.render("Hi {{company}}", f)
        with self.assertRaises(mailer.MergeError):
            mailer.render("Hi {{first_name", f)


if __name__ == "__main__":
    unittest.main()
