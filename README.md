# ColdLane

Free, self-hosted cold email for agencies. Connect your own mailboxes, import leads, and run multi-step sequences with mailbox rotation, safe daily caps and automatic stop on reply, bounce or unsubscribe. One-click unsubscribe, sender address and a global suppression list are built in, not bolted on.

No signup. No per-seat or per-mailbox fees. Docker Compose and a SQLite file. About 15 minutes on a 1GB VPS.

---

**What it is:** the core loop of Instantly / Smartlead / Lemlist on your own server. Rotate several SMTP/IMAP mailboxes, send a 1–5 step sequence, stop when someone replies, bounces or unsubscribes.

**What it is not:** no warmup network, no lead database, scraping, enrichment or email-finding, no open-tracking pixel, no click tracking or link rewriting, no AI. Bring your own leads and your own mailboxes.

> **Check your domains with SenderGrade first.** SPF, DKIM and DMARC must be aligned before you send anything.

## Contents

- [Install (Ubuntu 22.04 / 24.04)](#install-ubuntu-2204--2404)
- [Try it offline first (test profile + GreenMail)](#try-it-offline-first-test-profile--greenmail)
- [Production mailboxes](#production-mailboxes)
- [How sending works](#how-sending-works)
- [Environment variables](#environment-variables)
- [Compliance](#compliance)
- [Operations, tests and limitations](#operations-tests-and-limitations)

## Install (Ubuntu 22.04 / 24.04)

### 1. Docker + Compose

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER   # log out and back in, or prefix docker commands with sudo
```

**Debian 13 (trixie) note:** the distro packages work: `sudo apt-get install -y docker.io docker-compose git`. Debian's `docker-compose` package provides the v2 CLI as `docker-compose` (with a hyphen), so use `docker-compose ...` wherever this README says `docker compose ...`. If the `docker compose` plugin form is missing, that's expected.

### 2. Unpack and configure

Download the release zip, unzip it, and enter the folder:

```bash
unzip coldlane.zip
cd coldlane   # or the folder name inside the zip
cp .env.example .env
```

Generate the encryption key for mailbox passwords and paste it into `ENCRYPTION_KEY=` in `.env`:

```bash
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Then edit `.env`:

- `SECRET_KEY`: any long random string (`openssl rand -hex 32`).
- `ENCRYPTION_KEY`: the key from above. **Back it up.** If it changes, saved mailbox passwords can't be decrypted and must be re-entered. (If you leave it empty, ColdLane generates a key file next to the database and shows a warning banner. Fine for a trial, not for production.)
- `OWNER_PASSWORD`: your login.
- `PUBLIC_BASE_URL`: the public URL of this server, e.g. `https://coldlane.example.com`. It goes into every unsubscribe link, so **recipients must be able to reach it**. Campaigns can't start without it.
- `SENDER_BUSINESS_NAME` and `SENDER_ADDRESS`: your business name and **physical postal address** for the mandatory footer.

**Optional — install from git instead of the zip:**

```bash
git clone https://github.com/aidendify/coldlane.git
cd coldlane
cp .env.example .env
# then generate ENCRYPTION_KEY and edit .env as above
```

### 3. Start

```bash
docker compose up -d --build
docker compose ps                         # web should be "healthy"
curl -s http://127.0.0.1:8080/health      # {"status":"ok","worker_ok":true,...}
```

Open `http://YOUR_SERVER:8080` and log in with `OWNER_PASSWORD`. Two services run from one image: `web` (UI, port 8080) and `worker` (send loop + IMAP poll). They share the SQLite file on the `coldlane-data` volume.

### 4. HTTPS with Caddy (recommended)

Unsubscribe links should be HTTPS. Point a DNS record at the server, install Caddy (`sudo apt-get install -y caddy`), and use this `/etc/caddy/Caddyfile`:

```
coldlane.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

`sudo systemctl reload caddy`, then set `PUBLIC_BASE_URL=https://coldlane.example.com` and run `docker compose up -d`. Only `/health` and `/u/{token}` are public; everything else needs the owner login.

### 5. Outbound mail ports

ColdLane connects out to your mailbox provider on **587** (STARTTLS) or **465** (SSL), and to IMAP on **993**. Some VPS providers block outbound 25/587/465 by default. Check with your provider (or `nc -vz smtp.gmail.com 587`) before you debug anything else.

## Try it offline first (test profile + GreenMail)

The `test` profile adds [GreenMail](https://greenmail-mail-test.github.io/greenmail/), a local SMTP + IMAP catcher. Nothing leaves the server, any username/password works, and mailboxes are created automatically.

1. In `.env`, for fast feedback set:
   ```
   OWNER_PASSWORD=testpass
   PUBLIC_BASE_URL=http://127.0.0.1:8080
   SENDER_BUSINESS_NAME=Harbor Growth Agency
   SENDER_ADDRESS=12 Test Street, Testville, CA 90000, USA
   MIN_GAP_SECONDS=1
   MAX_GAP_SECONDS=2
   RECIPIENT_DOMAIN_PER_HOUR=100
   WORKER_TICK_SECONDS=5
   IMAP_POLL_SECONDS=10
   ```
2. `docker compose --profile test up --build -d`
3. **Mailboxes → Add mailbox**, twice (`a@agency.test` and `b@agency.test`):

   | Field | Value |
   | --- | --- |
   | From email | `a@agency.test` (then `b@agency.test`) |
   | SMTP host / port / security | `greenmail` / `3025` / `none` |
   | SMTP username | same as the from-address, e.g. `a@agency.test` |
   | SMTP password | anything, e.g. `test` |
   | IMAP host / port / SSL | `greenmail` / `3143` / **unchecked** |
   | IMAP username / password | blank (reuses the SMTP values) or the same values |
   | Daily cap / Ramp-up | `3` / **unchecked** (the defaults are 30 and on) |

   Save, then click **Test connection**: SMTP NOOP and IMAP SELECT INBOX should both say OK.
4. **New campaign**: tick both mailboxes, keep every day selected, window `0` to `24`. Step 1 subject `Quick question for {{company}}`, body `Hi {{first_name|there}}, ...`. Step 2: delay `1 minutes`, empty subject (sends as `Re:` in the same thread).
5. On the campaign page, import `sample-leads.csv` (10 `@example.test` leads) **with** the lawful-basis box ticked. Without it the import is rejected.
6. **Start campaign.** Within a few ticks, 6 emails go out, alternating a/b, 3 per mailbox. The other leads show `waiting on cap` (Leads → filter "waiting on cap").
7. Use the helper scripts inside the worker container:

   ```bash
   # Headers + body of what a lead received (List-Unsubscribe, footer, merge fields)
   docker compose exec worker python -m coldlane.scripts.dump_mailbox --user lead1@example.test

   # Reply from lead1 into mailbox a (Message-ID from dump_mailbox, the lead page or sends.csv)
   docker compose exec worker python -m coldlane.scripts.inject_reply --to a@agency.test --from lead1@example.test --in-reply-to '<MESSAGE-ID>'

   # Out-of-office auto-reply (adds Auto-Submitted: auto-replied); does NOT stop the sequence
   docker compose exec worker python -m coldlane.scripts.inject_reply --to b@agency.test --from lead2@example.test --in-reply-to '<MESSAGE-ID>' --auto-reply

   # Permanent bounce (DSN multipart/report, status 5.1.1)
   docker compose exec worker python -m coldlane.scripts.inject_bounce --to a@agency.test --failed lead3@example.test

   # One-click unsubscribe (token from the lead page or the List-Unsubscribe header)
   curl -X POST http://127.0.0.1:8080/u/<TOKEN>
   ```
8. Raise both caps to 10 to let step 2 flow. It goes from the same mailbox with `In-Reply-To` set, and never to the replied, bounced or unsubscribed leads.

The scripts read `GREENMAIL_HOST` / `GREENMAIL_SMTP_PORT` / `GREENMAIL_IMAP_PORT` (defaults `greenmail` / `3025` / `3143`). Stop the test server with `docker compose --profile test down` (add `-v` to wipe the data volume).

## Production mailboxes

ColdLane uses plain SMTP + IMAP with app passwords. Google/Microsoft OAuth is out of scope.

- **Google Workspace / Gmail:** turn on 2-Step Verification, create an app password at myaccount.google.com/apppasswords, then use `smtp.gmail.com:587 starttls` and `imap.gmail.com:993 SSL`.
- **Microsoft 365 / Outlook:** an admin must allow SMTP AUTH for the mailbox, then create an app password under Security info → App password; use `smtp.office365.com:587 starttls` and `outlook.office365.com:993 SSL`.

Passwords are encrypted at rest with Fernet (`ENCRYPTION_KEY`) and never shown again; leave the field blank when editing to keep the saved one. After **3 consecutive SMTP auth/connection failures** a mailbox is auto-paused, with the reason on the Mailboxes page. A passing **Test connection** clears the pause.

## How sending works

- **Worker tick** every `WORKER_TICK_SECONDS`. For each active campaign inside its send window (days + start/end hour in the campaign's timezone), due follow-ups go first, then new leads.
- **Due:** step N+1 is due when `now ≥ last_sent_at + delay`. Delays are minutes/hours/days in the UI and stored as minutes.
- **Right before every send** the global suppression list (email and domain) and the lead's status are re-checked.
- **Rotation:** a new lead gets the campaign mailbox with the most remaining capacity today (round-robin tie-break) and stays on it for every step.
- **Caps:** per mailbox `daily_cap`, default 30 and hard max 100. It counts across all campaigns per day (the container's timezone, UTC by default). **Ramp-up** (default on): effective cap = min(daily cap, `RAMP_START` + `RAMP_STEP` × days since the mailbox's first send). Leads that can't go out because of the cap show `waiting on cap` and log a `cap_skip` event.
- **Spacing:** a random `MIN_GAP_SECONDS`–`MAX_GAP_SECONDS` gap between sends from one mailbox, and at most `RECIPIENT_DOMAIN_PER_HOUR` sends per recipient domain per hour.
- **SMTP results:** a 5xx on RCPT/DATA is a hard bounce (lead `bounced`, address suppressed). A 4xx or timeout is retried, up to 3 attempts, then the lead goes to `error`. Every attempt is logged (`sends.csv`).
- **Bounce auto-pause:** once ≥20 emails have gone out in a campaign, a bounce rate (bounced leads ÷ emails) above `MAX_BOUNCE_RATE` pauses it with a banner.
- **Messages:** `multipart/alternative` (your plain text plus minimal HTML generated from it, no images, links untouched), your mailbox signature, then the system footer. Follow-ups with an empty subject use `Re: <step 1 subject>` with `In-Reply-To`/`References`.
- **Merge fields:** `{{first_name}}`, `{{company}}`, any CSV column, and fallbacks like `{{first_name|there}}`. A missing value with no fallback sets the lead to `merge_error`, so a literal `{{...}}` is never sent. **Preview** renders step 1 for the first 3 leads.
- **IMAP poll** every `IMAP_POLL_SECONDS`, read-only (`BODY.PEEK`, no flags, nothing deleted), from the last seen UID:
  - **Reply:** `In-Reply-To`/`References` match one of our Message-IDs, or the sender is an active lead. The lead becomes `replied` and stops in every campaign, and a 300-char snippet is stored.
  - **Auto-reply:** `Auto-Submitted`, `X-Autoreply`, `X-Autorespond` or `Precedence: auto_reply`. Logged as `auto_reply`; the sequence continues.
  - **Bounce:** a DSN (`multipart/report; report-type=delivery-status`) or a message from MAILER-DAEMON/postmaster. `5.x.x` sets `bounced` and suppresses the address; `4.x.x` is only logged.
  - **Opt-out reply:** a reply that just says "unsubscribe" / "remove me" means `unsubscribed` + suppression.
- **Start is blocked** unless the campaign has ≥1 active mailbox, ≥1 step, ≥1 queued lead, a sender postal address for every assigned mailbox, and `PUBLIC_BASE_URL` set.
- **Settings vs .env:** a business name or address saved in Settings overrides `.env`. A field saved **blank** in Settings counts as empty (it does not fall back to `.env`), so campaigns can't start. **Use .env values** removes the overrides. A per-mailbox address override beats both.
- **Rates on the dashboard:** reply rate = replied ÷ leads contacted; bounce rate = bounced ÷ emails that went out.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | 8080 | Web |
| `DATABASE_PATH` | `/data/coldlane.db` | SQLite (WAL) |
| `SECRET_KEY` | (none) | Sessions |
| `ENCRYPTION_KEY` | (none) | Fernet key for mailbox passwords (required for production) |
| `OWNER_PASSWORD` | (none) | Login |
| `PUBLIC_BASE_URL` | (none) | Unsubscribe links (required to start campaigns) |
| `SENDER_BUSINESS_NAME` / `SENDER_ADDRESS` | (none) | Footer (required to start campaigns unless set in Settings or per mailbox) |
| `DEFAULT_DAILY_CAP` | 30 | New-mailbox default (max 100) |
| `RAMP_START` / `RAMP_STEP` | 10 / 5 | Ramp-up |
| `MIN_GAP_SECONDS` / `MAX_GAP_SECONDS` | 90 / 240 | Per-mailbox spacing |
| `RECIPIENT_DOMAIN_PER_HOUR` | 10 | Per-domain throttle |
| `MAX_BOUNCE_RATE` | 0.05 | Campaign auto-pause |
| `WORKER_TICK_SECONDS` | 30 | Send loop |
| `IMAP_POLL_SECONDS` | 300 | Reply/bounce poll |
| `GREENMAIL_HOST` / `GREENMAIL_SMTP_PORT` / `GREENMAIL_IMAP_PORT` | greenmail / 3025 / 3143 | Test-profile helper scripts only |

## Compliance

ColdLane builds the mechanics in: every email has a `List-Unsubscribe` header pointing to `{PUBLIC_BASE_URL}/u/{token}` (plus a `mailto:`) and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058). `POST /u/{token}` unsubscribes instantly with no login or confirmation. The link in the body (`GET`) unsubscribes instantly and shows "You're unsubscribed" with an optional undo. A footer you can't remove carries your business name, physical postal address and the unsubscribe link. A global suppression list (emails and whole domains; sources `unsubscribe`, `bounce`, `manual`, `csv_import`, `reply_optout`) is checked at import, at enqueue and immediately before every send. A reply, hard bounce or unsubscribe stops that address in **all** campaigns. The defaults are conservative: 30/day/mailbox, ramp-up, gaps, a domain throttle and bounce auto-pause. Every import requires a lawful-basis confirmation, which is stored with who, when and the filename. There are **no** tracking pixels, click tracking or link rewriting ("Open/click tracking: off. ColdLane does not track opens or clicks."), and no warmup network, scraping, enrichment or email-finding.

How that maps to the rules:

- **CAN-SPAM (US):** use accurate header information (real From name and address, honest routing) and non-deceptive subject lines. Include your valid physical postal address (the footer). Give a clear way to opt out and honor it within 10 business days. ColdLane does it instantly and permanently via the suppression list.
- **GDPR / PECR (EU/UK):** you need a lawful basis, typically legitimate interest for B2B outreach, and you should document your legitimate-interest assessment. B2B rules vary by country: some EU states require prior consent even for business addresses, and under PECR sole traders and some partnerships count as individuals. Honor the right to object: an unsubscribe or opt-out reply goes onto the global suppression list. Practice data minimization: import only the fields you need, and delete leads and campaigns you no longer need.
- **Gmail / Yahoo bulk-sender rules:** authenticate with SPF and DKIM and publish DMARC with alignment to your From domain (SenderGrade checks this). Support one-click unsubscribe (built in) and keep spam complaint rates under 0.3% (aim far lower: send small volumes to well-targeted lists).

**You, the operator, are responsible for lawful use. ColdLane is not legal advice.**

## Operations, tests and limitations

- **Data:** everything is in `/data/coldlane.db` (SQLite, WAL) on the `coldlane-data` volume. Back it up with `docker compose exec web python -c "import sqlite3;s=sqlite3.connect('/data/coldlane.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"` and then `docker compose cp web:/data/backup.db .`.
- **Exports:** `leads.csv` per campaign (with status), `suppression.csv` and `sends.csv` (send log). Timestamps are UTC.
- **Health:** `GET /health` returns `{"status":"ok","worker_last_tick":"...","worker_ok":true,"mailboxes_active":N}`. `worker_ok` is false if the worker hasn't ticked within 3 × `WORKER_TICK_SECONDS`.
- **Logs:** `docker compose logs -f worker`.
- **Tests:** `pip install -r requirements-dev.txt && python -m unittest discover -s tests -v` (they use a local aiosmtpd SMTP server and a fake IMAP).
- **Limitations:** one owner login (no multi-user). No inbox or reply-from-app: replies show as snippets only. No OAuth. The send window uses whole hours. Daily caps reset at midnight in the container's timezone (UTC by default). Removing an address from suppression doesn't automatically requeue leads that were already stopped.
