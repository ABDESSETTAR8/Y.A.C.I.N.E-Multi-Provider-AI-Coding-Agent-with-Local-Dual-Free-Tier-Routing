#!/usr/bin/env python
"""Console entry point for Y.A.C.I.N.E.

Usage:
    python main.py chat [--role coder]
    python main.py run "build a flask api" [--project ./myapp] [--verify "pytest -q"]
    python main.py serve [--host 127.0.0.1 --port 8765]
    python main.py doctor
    python main.py models
"""
import sys

from yacine.cli import main

if __name__ == "__main__":
    sys.exit(main())
