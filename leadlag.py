#!/usr/bin/env python3
"""Compatibility entry point for the legacy lead/lag analyzer."""
from utils.legacy_leadlag import *  # noqa: F401,F403
from utils.legacy_leadlag import main

if __name__ == "__main__":
    main()
