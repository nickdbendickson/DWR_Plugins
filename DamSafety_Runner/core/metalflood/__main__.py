"""python -m <package>.metalflood run DECK.par [--solver ...] [--backend metal|cpu]  (see cli.py)

The older form `python -m <package>.metalflood run.par [-backend metal|cpu] [-v]` runs the ACC
solver as before."""
import sys

from .cli import main

sys.exit(main(sys.argv))
