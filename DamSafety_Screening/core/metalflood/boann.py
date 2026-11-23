#!/usr/bin/env python3
"""Boann's standalone launcher: run this file with any Python 3 that has numpy, no QGIS.

    python3 boann.py run DECK.par [--solver acc|sgc|fv|fv2|dg2|subfv] [--backend auto|metal|cpu]
    python3 boann.py check | solvers | version | --help

It loads this folder as a package named `boann` (nothing outside the folder is imported) and hands
over to cli.main. See cli.py and README.md."""
import importlib.util
import os
import sys


def _load():
    here = os.path.dirname(os.path.abspath(__file__))
    if __package__:                       # imported or run with -m inside a plugin: use the package
        return importlib.import_module(__package__ + ".cli")
    # run as a script: Python put this folder first on sys.path, where engine.py / cases.py ...
    # could shadow other modules' imports; take it off and load the folder as package `boann`.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    if "boann" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "boann", os.path.join(here, "__init__.py"), submodule_search_locations=[here])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["boann"] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module("boann.cli")


if __name__ == "__main__":
    sys.exit(_load().main(["boann"] + sys.argv[1:]))
