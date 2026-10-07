"""Print headers and decoded bodies of every message a lead received (GreenMail IMAP, read-only).

Usage:
  python -m coldlane.scripts.dump_mailbox --user lead1@example.test
"""
import argparse
import email
import imaplib
import sys
from email import policy

from coldlane.scripts import greenmail_host, greenmail_imap_port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True, help="lead address (GreenMail login)")
    parser.add_argument("--password", default="any", help="any value works with GreenMail auth disabled")
    parser.add_argument("--raw", action="store_true", help="print the raw RFC 5322 source instead")
    args = parser.parse_args(argv)
    client = imaplib.IMAP4(greenmail_host(), greenmail_imap_port(), timeout=30)
    client.login(args.user, args.password)
    try:
        typ, data = client.select("INBOX", readonly=True)
        count = int(data[0]) if typ == "OK" else 0
        print(f"Mailbox {args.user}: {count} message(s)")
        if not count:
            return 0
        typ, data = client.uid("SEARCH", None, "ALL")
        for n, uid in enumerate(data[0].split(), 1):
            typ, fetched = client.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next(item[1] for item in fetched if isinstance(item, tuple))
            print(f"\n==================== Message {n} (UID {uid.decode()}) ====================")
            if args.raw:
                print(raw.decode(errors="replace"))
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            for name, value in msg.items():
                print(f"{name}: {value}")
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                print(f"\n----- {part.get_content_type()} -----")
                try:
                    print(part.get_content())
                except Exception:  # noqa: BLE001
                    print(part.get_payload(decode=True).decode(errors="replace"))
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
