#!/usr/bin/env python3
"""
Convenience launcher so you don't have to be in the project root or use -m:

    python F:\\HyruleLink\\run_agent.py --setup     # first-time login/join
    python F:\\HyruleLink\\run_agent.py             # connect + run

Adds the project root to sys.path, then hands off to agent.main.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.main import main  # noqa: E402

if __name__ == "__main__":
    main()
