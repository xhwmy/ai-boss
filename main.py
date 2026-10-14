"""Compatibility entry point for the supported web engine.

The old standalone CLI used browser methods that no longer exist.  Running
this file now starts the same guarded engine and local control panel as app.py.
"""

import runpy


if __name__ == "__main__":
    runpy.run_module("app", run_name="__main__")
