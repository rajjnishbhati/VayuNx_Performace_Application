"""VAYUNX Crypto Lab - benchmark built-in crypto presets without any app code.

Only built-in presets with validated parameters can run (presets.PRESETS). Each trial runs in a
fresh subprocess (worker.py); runner.py interleaves variants across trials and stores results.
Every number the Lab produces comes from a run on the machine it ran on; see env.py.
"""

__version__ = "0.2.0"
