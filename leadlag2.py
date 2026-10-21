#!/usr/bin/env python3
"""Compatibility entry point for the lead/lag analysis in :mod:`utils.leadlag`."""
from utils.leadlag import *  # noqa: F401,F403
from utils.leadlag import main

if __name__ == "__main__":
    main()
