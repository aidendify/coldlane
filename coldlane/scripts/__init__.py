"""Verifier helper scripts for the offline GreenMail test profile."""
import os


def greenmail_host():
    return os.environ.get("GREENMAIL_HOST") or "greenmail"


def greenmail_smtp_port():
    return int(os.environ.get("GREENMAIL_SMTP_PORT") or 3025)


def greenmail_imap_port():
    return int(os.environ.get("GREENMAIL_IMAP_PORT") or 3143)
