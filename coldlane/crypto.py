"""Fernet encryption for mailbox passwords at rest."""
import os
import tempfile

from cryptography.fernet import Fernet, InvalidToken

from . import config

_cache = {"key": None, "generated": False, "source": None}

KEY_FILENAME = ".encryption_key"


def reset_cache():
    _cache.update({"key": None, "generated": False, "source": None})


def _key_file_path():
    return os.path.join(os.path.dirname(os.path.abspath(config.db_path())), KEY_FILENAME)


def _load_or_create_key_file():
    """Fallback when ENCRYPTION_KEY is unset: create a key file next to the DB once.

    Uses an atomic link so the web and worker services agree on one key.
    """
    path = _key_file_path()
    if not os.path.exists(path):
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".keytmp")
        try:
            os.write(fd, Fernet.generate_key())
            os.close(fd)
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    with open(path, "rb") as handle:
        return handle.read().strip().decode()


def get_key():
    if _cache["key"]:
        return _cache["key"]
    key = config.env("ENCRYPTION_KEY")
    if key:
        try:
            Fernet(key.encode())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python3 -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
            ) from exc
        _cache.update({"key": key, "generated": False, "source": "env"})
    else:
        _cache.update({"key": _load_or_create_key_file(), "generated": True, "source": _key_file_path()})
    return _cache["key"]


def using_generated_key():
    get_key()
    return _cache["generated"]


def encrypt(plaintext):
    if plaintext is None or plaintext == "":
        return None
    return Fernet(get_key().encode()).encrypt(plaintext.encode()).decode()


def decrypt(token):
    if not token:
        return ""
    try:
        return Fernet(get_key().encode()).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Cannot decrypt mailbox password: ENCRYPTION_KEY changed since it was saved. Re-enter the password.") from exc
