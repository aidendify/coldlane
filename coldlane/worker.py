"""Worker service: `python -m coldlane.worker`. Send loop + IMAP poll + heartbeat."""
import signal
import sys
import time
import traceback

from . import config, core, crypto, db, engine

_running = {"value": True}


def _stop(signum, frame):  # noqa: ARG001
    _running["value"] = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.init_db()
    crypto.get_key()
    if crypto.using_generated_key():
        print("WARNING: ENCRYPTION_KEY is not set; using an auto-generated key file next to the database. "
              "Set ENCRYPTION_KEY in .env for production.", flush=True)
    print(f"ColdLane worker started: tick={config.tick_seconds()}s imap_poll={config.imap_poll_seconds()}s "
          f"db={config.db_path()}", flush=True)
    while _running["value"]:
        started = time.monotonic()
        conn = db.connect()
        try:
            sent, polled = engine.tick(conn)
            if sent or polled:
                print(f"{core.ts(core.utcnow())} tick: sent={sent} imap_messages={polled}", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            conn.close()
        remaining = config.tick_seconds() - (time.monotonic() - started)
        while remaining > 0 and _running["value"]:
            time.sleep(min(1.0, remaining))
            remaining -= 1.0
    print("ColdLane worker stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
