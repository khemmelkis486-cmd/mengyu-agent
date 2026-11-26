"""Run the exported unit suites without an AstrBot installation."""

import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "plugins"
sys.path.insert(0, str(PLUGINS))


if __name__ == "__main__":
    suites = [
        unittest.TestLoader().discover(str(PLUGINS / plugin / "tests"))
        for plugin in ("mengyu_contact", "mengyu_task_center", "mengyu_economy")
    ]
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(suites))
    raise SystemExit(not result.wasSuccessful())
