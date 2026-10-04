# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run the PySide widget tests headlessly with FreeCAD's own Qt, for example:

    flatpak run --env=QT_QPA_PLATFORM=offscreen --command=FreeCADCmd \\
        org.freecad.FreeCAD tests/ai_assistant/qt_tests.py
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6 import QtWidgets  # noqa: E402  FreeCAD 1.x ships PySide6.

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
import test_chat_view  # noqa: E402

result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromModule(test_chat_view))
print("QT TESTS: {} run, {} failed, {} errors, {} skipped".format(
    result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
