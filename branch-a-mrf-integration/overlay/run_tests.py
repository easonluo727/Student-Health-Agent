"""Run the overlay test suite:  python branch-a-mrf-integration/overlay/run_tests.py"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tests"))
suite = unittest.defaultTestLoader.discover(str(HERE / "tests"), top_level_dir=str(HERE / "tests"))
sys.exit(0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1)
