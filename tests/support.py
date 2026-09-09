"""Locate the launcher module. Tests import it, they do not copy it."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "launcher"))

import satoru  # noqa: E402  (path juggling has to come first)


def has_tomllib():
    try:
        import tomllib  # noqa: F401
        return True
    except ImportError:
        return False
