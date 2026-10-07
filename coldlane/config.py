"""Environment configuration. Values are read at call time so tests can override them."""
import os

HARD_MAX_DAILY_CAP = 100


def env(name, default=None):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_int(name, default):
    try:
        return int(env(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(env(name, default))
    except (TypeError, ValueError):
        return default


def db_path():
    return env("DATABASE_PATH", "/data/coldlane.db")


def public_base_url():
    url = env("PUBLIC_BASE_URL", "")
    return url.rstrip("/") if url else ""


def default_daily_cap():
    cap = env_int("DEFAULT_DAILY_CAP", 30)
    return max(1, min(cap, HARD_MAX_DAILY_CAP))


def ramp_start():
    return env_int("RAMP_START", 10)


def ramp_step():
    return env_int("RAMP_STEP", 5)


def min_gap():
    return max(0, env_int("MIN_GAP_SECONDS", 90))


def max_gap():
    return max(min_gap(), env_int("MAX_GAP_SECONDS", 240))


def domain_per_hour():
    return env_int("RECIPIENT_DOMAIN_PER_HOUR", 10)


def max_bounce_rate():
    return env_float("MAX_BOUNCE_RATE", 0.05)


def tick_seconds():
    return max(1, env_int("WORKER_TICK_SECONDS", 30))


def imap_poll_seconds():
    return max(1, env_int("IMAP_POLL_SECONDS", 300))
