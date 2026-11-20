"""Your shared defaults file, for the Crest desktop window.

The dam tools share one defaults file format ("dam-defaults", DamSafety Core `damsuite/shared_defaults.py`):
the suite ships its values, and YOUR file holds only the keys you changed. The desk (src/lib/crest/settings.ts)
knows the keys, the suite's values and the merge; this module only finds your file, reads its text and writes it
back, so there is one copy of the rules.

Your file: env CREST_DEFAULTS, else env DAMSAFETY_DEFAULTS, else ~/DamSafety/defaults.json. It need not exist.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FORMAT = "dam-defaults"
FORMAT_VERSION = 1
FILE_NAME = "defaults.json"
USER_FOLDER = "DamSafety"
ENV_PATHS = ("CREST_DEFAULTS", "DAMSAFETY_DEFAULTS")
#: A defaults file is a few hundred bytes. Anything this large is not one, and is never read into the window.
MAX_BYTES = 200_000


def user_path() -> Path:
    """Where your defaults file is (it may not exist yet)."""
    for name in ENV_PATHS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / USER_FOLDER / FILE_NAME


def read_user(path: Path | None = None) -> dict:
    """{"path", "text"}: your file's text, or text None when there is no file. {"path", "error"} when it cannot be
    read. The desk parses and checks the text (settings.ts readDefaultsFile)."""
    path = path or user_path()
    try:
        if not path.is_file():
            return {"path": str(path), "text": None}
        if path.stat().st_size > MAX_BYTES:
            return {"path": str(path), "error": f"{path} is too large to be a defaults file."}
        return {"path": str(path), "text": path.read_text(encoding="utf-8-sig")}
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": str(path), "error": f"The defaults file {path} cannot be read: {exc}"}


def check_text(text) -> str | None:
    """Why this text must not be written as a defaults file, or None. Only the shape is checked here: the desk has
    already checked every key and value."""
    if not isinstance(text, str):
        return "The defaults were not text."
    if len(text.encode("utf-8")) > MAX_BYTES:
        return "The defaults are too large to be a defaults file."
    try:
        data = json.loads(text)
    except ValueError as exc:
        return f"The defaults are not valid JSON: {exc}"
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return f'The defaults are not in the "{FORMAT}" format.'
    if data.get("format_version") != FORMAT_VERSION:
        return f"The defaults are not format version {FORMAT_VERSION}."
    values = data.get("values")
    if not isinstance(values, dict):
        return "The defaults have no 'values' object."
    for key, value in values.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return f"The value of '{key}' is not text or a number."
    return None


def write_user(text, path: Path | None = None) -> dict:
    """Write your defaults file (atomically: a crash mid-write must not leave half a file for the plugins to read).
    {"saved": path} or {"error": why}."""
    path = path or user_path()
    problem = check_text(text)
    if problem:
        return {"error": problem}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"error": f"The defaults file {path} cannot be written: {exc}"}
    return {"saved": str(path)}
