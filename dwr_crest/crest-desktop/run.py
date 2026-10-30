"""Desktop entry. `python run.py` from this folder (Python 3.10–3.12)."""

import os
import sys

if not ((3, 10) <= sys.version_info[:2] <= (3, 12)) and not os.environ.get("CREST_ANY_PYTHON"):
    sys.exit(
        f"Crest desktop needs Python 3.10–3.12 (this is {sys.version.split()[0]}). "
        "PyInstaller fails on 3.13. Set CREST_ANY_PYTHON=1 to try anyway."
    )

from crest.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
