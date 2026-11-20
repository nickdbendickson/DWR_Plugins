"""Desktop entry. `python run.py` from this folder (Python 3.10 or newer)."""

import sys

if sys.version_info[:2] < (3, 10):
    sys.exit(f"Crest desktop needs Python 3.10 or newer (this is {sys.version.split()[0]}).")

from crest.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
