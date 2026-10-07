"""Message rendering (merge fields, footer, headers) and SMTP/IMAP transport."""
import html
import imaplib
import re
import smtplib
import socket
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

from . import core, crypto

MERGE_RE = re.compile(r"\{\{\s*([^{}|]+?)\s*(?:\|([^{}]*))?\}\}")
SMTP_TIMEOUT = 30
IMAP_TIMEOUT = 30


class MergeError(Exception):
    pass


class MailboxError(Exception):
    """SMTP auth/connection failure: counts toward mailbox auto-pause."""


class HardBounce(Exception):
    """5xx on RCPT/DATA."""


class SoftFail(Exception):
    """4xx or timeout during the transaction: retried later."""


# ---------------------------------------------------------------- merge fields
def render(template, fields):
    """Render {{field}} and {{field|fallback}}. Missing value without fallback raises MergeError."""
    missing = []

    def replace(match):
        key = core.norm_key(match.group(1))
        value = fields.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
        if match.group(2) is not None:
            return match.group(2).strip()
        missing.append(key)
        return ""

    output = MERGE_RE.sub(replace, template or "")
    if missing:
        raise MergeError("missing merge field(s) with no fallback: " + ", ".join(sorted(set(missing))))
    if "{{" in output or "}}" in output:
        raise MergeError("unbalanced merge braces in template")
    return output


# ---------------------------------------------------------------- message building
def footer_text(business, address, unsub_url):
    lines = [line for line in [business.strip()] if line]
    lines.extend(part.strip() for part in address.strip().splitlines() if part.strip())
    lines.append(f"Don't want these emails? Unsubscribe: {unsub_url}")
    return "\n".join(lines)


def html_from_text(body_text, footer, unsub_url):
    """Minimal HTML from plain text: escaped, line breaks only. No images, no rewritten links."""
    body_html = html.escape(body_text).replace("\n", "<br>\n")
    foot_lines = footer.splitlines()[:-1]
    foot_html = "<br>\n".join(html.escape(line) for line in foot_lines)
    safe_url = html.escape(unsub_url, quote=True)
    return (
        "<html><body>\n"
        f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">{body_html}</div>\n'
        '<hr style="border:none;border-top:1px solid #ddd;margin:16px 0">\n'
        f'<div style="font-family:Arial,sans-serif;font-size:12px;color:#666">{foot_html}<br>\n'
        f"Don't want these emails? Unsubscribe: <a href=\"{safe_url}\">{safe_url}</a></div>\n"
        "</body></html>\n"
    )


def build_message(mb, to_email, subject, body, unsub_url, business, address,
                  in_reply_to=None, references=None, now=None):
    """Return (EmailMessage, message_id)."""
    footer = footer_text(business, address, unsub_url)
    body_with_sig = body.rstrip()
    if mb["signature"] and mb["signature"].strip():
        body_with_sig += "\n\n" + mb["signature"].rstrip()
    text = body_with_sig + "\n\n--\n" + footer + "\n"
    msg = EmailMessage(policy=policy.SMTP)
    domain = core.domain_of(mb["from_email"])
    message_id = make_msgid(domain=domain)
    msg["From"] = formataddr((mb["from_name"] or "", mb["from_email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = format_datetime(now or core.utcnow())
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:{mb['from_email']}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html_from_text(body_with_sig, footer, unsub_url), subtype="html")
    return msg, message_id


# ---------------------------------------------------------------- SMTP
def smtp_open(mb, timeout=SMTP_TIMEOUT):
    host, port, security = mb["smtp_host"], int(mb["smtp_port"]), (mb["smtp_security"] or "starttls")
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if mb["smtp_user"] and server.has_extn("auth"):
            server.login(mb["smtp_user"], crypto.decrypt(mb["smtp_pass_enc"]))
        return server
    except smtplib.SMTPAuthenticationError as exc:
        raise MailboxError(f"SMTP auth failed: {exc.smtp_code} {exc.smtp_error.decode(errors='replace')}") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError, RuntimeError) as exc:
        raise MailboxError(f"SMTP connection to {host}:{port} failed: {exc.__class__.__name__}: {exc}") from exc


def _close(server):
    try:
        server.quit()
    except Exception:  # noqa: BLE001
        try:
            server.close()
        except Exception:  # noqa: BLE001
            pass


def send_message(mb, msg, rcpt):
    """Send one message. Returns the final SMTP reply string or raises HardBounce/SoftFail/MailboxError."""
    server = smtp_open(mb)
    try:
        code, resp = server.mail(mb["from_email"])
        text = f"{code} {resp.decode(errors='replace')}"
        if code >= 500:
            raise MailboxError(f"MAIL FROM refused: {text}")
        if code >= 400:
            raise SoftFail(f"MAIL FROM deferred: {text}")
        code, resp = server.rcpt(rcpt)
        text = f"{code} {resp.decode(errors='replace')}"
        if code >= 500:
            raise HardBounce(f"RCPT {text}")
        if code >= 400:
            raise SoftFail(f"RCPT {text}")
        try:
            code, resp = server.data(msg.as_bytes(policy=policy.SMTP))
        except smtplib.SMTPDataError as exc:
            code, resp = exc.smtp_code, exc.smtp_error
        text = f"{code} {resp.decode(errors='replace') if isinstance(resp, bytes) else resp}"
        if code >= 500:
            raise HardBounce(f"DATA {text}")
        if code >= 400:
            raise SoftFail(f"DATA {text}")
        return text
    except (socket.timeout, TimeoutError) as exc:
        raise SoftFail(f"timeout: {exc}") from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise SoftFail(f"server disconnected: {exc}") from exc
    finally:
        _close(server)


# ---------------------------------------------------------------- IMAP
def imap_open(mb, timeout=IMAP_TIMEOUT):
    host, port = mb["imap_host"], int(mb["imap_port"])
    if mb["imap_ssl"]:
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    else:
        client = imaplib.IMAP4(host, port, timeout=timeout)
    client.login(mb["imap_user"] or mb["smtp_user"] or mb["from_email"], crypto.decrypt(mb["imap_pass_enc"]))
    return client


def test_connection(mb):
    """SMTP login + NOOP and IMAP login + SELECT INBOX. Returns (ok, [messages])."""
    results, ok = [], True
    try:
        server = smtp_open(mb)
        try:
            code, resp = server.noop()
            results.append(f"SMTP OK: NOOP {code} {resp.decode(errors='replace')}")
        finally:
            _close(server)
    except Exception as exc:  # noqa: BLE001
        ok = False
        results.append(f"SMTP FAILED: {exc}")
    try:
        client = imap_open(mb)
        try:
            typ, data = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError(f"SELECT INBOX returned {typ} {data}")
            results.append(f"IMAP OK: INBOX has {data[0].decode(errors='replace')} message(s)")
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        ok = False
        results.append(f"IMAP FAILED: {exc.__class__.__name__}: {exc}")
    return ok, results
