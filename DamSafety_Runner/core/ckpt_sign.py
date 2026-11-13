"""Signed ANUGA checkpoints (Runner 3.20.3, security audit pass 3).

An ANUGA checkpoint is a pickle of the whole domain (ANUGA writes it with dill), and loading a
pickle runs whatever code the file holds. A checkpoint folder that came from somewhere else -
a dam folder a colleague zipped, a shared drive, a download - could therefore run anything as
the user who resumes it. A pickle of this size and shape cannot be loaded "safely", so the
worker loads only checkpoints it can prove this user's own runs wrote:

* when the worker saves a checkpoint it writes <checkpoint>.sig beside it: an HMAC-SHA256 of
  the file under a secret key kept in the user's own settings folder (never in a dam folder);
* before a checkpoint is loaded its signature is checked. No signature, a signature made with
  another key (another computer or user), or a file changed since it was signed: not loaded.

A checkpoint the user knows is their own (made before 3.20.3, or on their other computer) can
still be resumed by setting NDBREACH_ANUGA_RESUME_TRUST=1 for that run; the worker says so in
the log. Pure Python, no ANUGA, no QGIS.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys

SIG_SUFFIX = ".sig"
ALG = "HMAC-SHA256"
KEY_FILE_ENV = "NDBREACH_CKPT_KEY_FILE"         # tests, or a key shared between your computers
TRUST_ENV = "NDBREACH_ANUGA_RESUME_TRUST"       # 1 = load a checkpoint that does not verify
KEY_NAME = "anuga_checkpoint.key"
APP_DIR = "DamSafety"
_CHUNK = 4 * 1024 * 1024


def settings_dir(environ=None, platform=None):
    """The user's own settings folder for the suite (not created here)."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    home = os.path.expanduser("~")
    if plat.startswith("win"):
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif plat == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, APP_DIR)


def key_path(environ=None):
    env = os.environ if environ is None else environ
    return env.get(KEY_FILE_ENV) or os.path.join(settings_dir(env), KEY_NAME)


def _read_key(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(4096).strip()
    except OSError:
        return None
    try:
        key = bytes.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    return key if len(key) >= 32 else None


def load_key(create=False, environ=None):
    """The signing key (bytes), or None when there is none and create is False (or it cannot
    be written). A new key is 32 random bytes, written for this user only (0600 in a 0700
    folder where the system has such modes), through an exclusive create so two runs that
    start together end with one key."""
    path = key_path(environ)
    key = _read_key(path)
    if key is not None or not create:
        return key
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key(path)              # another run made it first (or it is damaged)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_hex(32).encode("ascii") + b"\n")
    except OSError:
        return None
    return _read_key(path)


def _mac(path, key):
    h = hmac.new(key, digestmod=hashlib.sha256)
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            size += len(block)
            h.update(block)
    return h.hexdigest(), size


def sig_path(path):
    return path + SIG_SUFFIX


def sign(path, environ=None):
    """Write <path>.sig for the checkpoint at `path`. '' when signed, else why not (the run
    goes on; that checkpoint cannot be resumed without NDBREACH_ANUGA_RESUME_TRUST=1)."""
    key = load_key(create=True, environ=environ)
    if key is None:
        return "no signing key could be read or written at {}".format(key_path(environ))
    try:
        mac, size = _mac(path, key)
        tmp = sig_path(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"alg": ALG, "mac": mac, "size": size}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sig_path(path))
        return ""
    except OSError as e:
        return "{}: {}".format(type(e).__name__, str(e)[:200])


def verify(path, environ=None):
    """(True, '') when the checkpoint at `path` was signed with this user's key and has not
    changed since; else (False, why)."""
    try:
        with open(sig_path(path), encoding="utf-8") as f:
            sig = json.load(f)
    except OSError:
        return False, "it has no signature (written before Runner 3.20.3, or not by the Runner)"
    except ValueError:
        return False, "its signature file cannot be read"
    if not isinstance(sig, dict) or sig.get("alg") != ALG or not isinstance(sig.get("mac"), str):
        return False, "its signature file is not one the Runner writes"
    key = load_key(create=False, environ=environ)
    if key is None:
        return False, ("this user has no checkpoint key ({}): the checkpoint was not written "
                       "by this user on this computer".format(key_path(environ)))
    try:
        mac, size = _mac(path, key)
    except OSError as e:
        return False, "it cannot be read ({})".format(str(e)[:120])
    if not hmac.compare_digest(mac, sig["mac"]):
        if isinstance(sig.get("size"), int) and sig["size"] != size:
            return False, "the file has changed since it was signed"
        return False, ("the file has changed since it was signed, or it was signed by another "
                       "user or computer")
    return True, ""


def trusted(environ=None):
    """True when the user has said, for this run, to load checkpoints that do not verify."""
    env = os.environ if environ is None else environ
    return str(env.get(TRUST_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def refusal(path, why):
    """The message for a checkpoint that is not loaded."""
    return ("{} was not loaded: {}. A checkpoint is a Python pickle, and loading one runs the "
            "code it holds, so the Runner loads only checkpoints its own runs wrote for this "
            "user. If this checkpoint is your own, set {}=1 for this run to load it "
            "anyway.".format(os.path.basename(path), why, TRUST_ENV))


def remove_sig(path):
    try:
        os.remove(sig_path(path))
    except OSError:
        pass
