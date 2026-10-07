"""Send a reply (or auto-reply) from a lead into a mailbox via GreenMail SMTP.

Usage:
  python -m coldlane.scripts.inject_reply --to a@agency.test --from lead1@example.test --in-reply-to '<id@agency.test>'
  python -m coldlane.scripts.inject_reply --to a@agency.test --from lead2@example.test --in-reply-to '<id>' --auto-reply
"""
import argparse
import smtplib
import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from coldlane.core import utcnow
from coldlane.scripts import greenmail_host, greenmail_smtp_port


def build(args):
    msg = EmailMessage()
    msg["From"] = args.sender
    msg["To"] = args.to
    msg["Date"] = format_datetime(utcnow())
    msg["Message-ID"] = make_msgid(domain=args.sender.rsplit("@", 1)[-1])
    if args.in_reply_to:
        mid = args.in_reply_to.strip()
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        msg["In-Reply-To"] = mid
        msg["References"] = mid
    if args.auto_reply:
        msg["Subject"] = args.subject or "Automatic reply: Out of office"
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-Autoreply"] = "yes"
        msg.set_content(args.body or "I'm out of the office until Monday with limited access to email.")
    else:
        msg["Subject"] = args.subject or "Re: your email"
        msg.set_content(args.body or "Thanks for reaching out. Yes, happy to chat next week. What times work?")
    return msg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", required=True, help="mailbox address that receives the reply")
    parser.add_argument("--from", dest="sender", required=True, help="lead address the reply comes from")
    parser.add_argument("--in-reply-to", default="", help="Message-ID of the email being replied to")
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--auto-reply", action="store_true",
                        help="add Auto-Submitted: auto-replied + X-Autoreply headers (out-of-office)")
    args = parser.parse_args(argv)
    msg = build(args)
    with smtplib.SMTP(greenmail_host(), greenmail_smtp_port(), timeout=30) as smtp:
        smtp.send_message(msg, from_addr=args.sender, to_addrs=[args.to])
    kind = "auto-reply" if args.auto_reply else "reply"
    print(f"Injected {kind} from {args.sender} to {args.to} (In-Reply-To: {msg.get('In-Reply-To', '-')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
