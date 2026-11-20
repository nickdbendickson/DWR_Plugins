"""python -m nd_breach_runner.core.metalflood run.par [-backend metal|cpu] [-v]"""
import sys

from .engine import main

sys.exit(main(sys.argv))
