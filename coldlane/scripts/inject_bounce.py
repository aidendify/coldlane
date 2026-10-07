"""Send a DSN bounce (multipart/report; report-type=delivery-status) into a mailbox via GreenMail SMTP.

Usage:
  python -m coldlane.scripts.inject_bounce --to a@agency.test --failed lead3@example.test
"""
import argparse
import smtplib
import sys
import uuid
from email.utils import format_datetime, make_msgid

from coldlane.core import utcnow
from coldlane.scripts import greenmail_host, greenmail_smtp_port


def build_dsn(to_addr, failed, status="5.1.1", original_message_id="", reporting_domain=None):
    domain = reporting_domain or to_addr.rsplit("@", 1)[-1]
    boundary = "dsn-" + uuid.uuid4().hex
    diag = "550 5.1.1 The email account that you tried to reach does not exist" if status.startswith("5") \
        else f"450 {status} Mailbox temporarily unavailable"
    action = "failed" if status.startswith("5") else "delayed"
    orig_headers = f"To: {failed}\r\nFrom: {to_addr}\r\nSubject: (original message)\r\n"
    if original_message_id:
        orig_headers += f"Message-ID: {original_message_id}\r\n"
    lines = [
        f"From: Mail Delivery System <MAILER-DAEMON@{domain}>",
        f"To: {to_addr}",
        "Subject: Undelivered Mail Returned to Sender",
        f"Date: {format_datetime(utcnow())}",
        f"Message-ID: {make_msgid(domain=domain)}",
        "Auto-Submitted: auto-replied",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/report; report-type=delivery-status; boundary="{boundary}"',
        "",
        "This is a MIME-encapsulated delivery status notification.",
        "",
        f"--{boundary}",
        "Content-Type: text/plain; charset=us-ascii",
        "",
        f"Delivery to the following recipient failed permanently:\r\n\r\n    {failed}\r\n\r\n{diag}",
        "",
        f"--{boundary}",
        "Content-Type: message/delivery-status",
        "",
        f"Reporting-MTA: dns; mx.{domain}",
        "",
        f"Final-Recipient: rfc822; {failed}",
        f"Action: {action}",
        f"Status: {status}",
        f"Diagnostic-Code: smtp; {diag}",
        "",
        f"--{boundary}",
        "Content-Type: text/rfc822-headers",
        "",
        orig_headers,
        f"--{boundary}--",
        "",
    ]
    return "\r\n".join(lines).encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", required=True, help="mailbox address that receives the bounce")
    parser.add_argument("--failed", required=True, help="lead address that bounced")
    parser.add_argument("--status", default="5.1.1", help="enhanced status code (default 5.1.1 permanent)")
    parser.add_argument("--message-id", default="", help="optional original Message-ID")
    args = parser.parse_args(argv)
    raw = build_dsn(args.to, args.failed, args.status, args.message_id)
    with smtplib.SMTP(greenmail_host(), greenmail_smtp_port(), timeout=30) as smtp:
        smtp.sendmail(f"MAILER-DAEMON@{args.to.rsplit('@', 1)[-1]}", [args.to], raw)
    print(f"Injected DSN bounce (status {args.status}) for {args.failed} into {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
