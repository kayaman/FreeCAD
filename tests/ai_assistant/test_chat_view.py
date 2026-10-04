# SPDX-License-Identifier: LGPL-2.1-or-later
"""Chat widget tests. They need PySide; run them headlessly with qt_tests.py."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
try:
    from freecad_ai import chat_view as cv
    from freecad_ai.chat_view import QtCore, QtGui, QtWidgets
except ImportError:  # No PySide on this interpreter (e.g. the host's plain Python).
    cv = None


def palette(dark):
    colors = {"Window": "#2b2b2b", "WindowText": "#e6e6e6", "Base": "#1f1f1f",
              "Text": "#e6e6e6", "Highlight": "#3d8fd6", "HighlightedText": "#ffffff"} if dark \
        else {"Window": "#efefef", "WindowText": "#1f1f1f", "Base": "#ffffff",
              "Text": "#1f1f1f", "Highlight": "#2f6fbf", "HighlightedText": "#ffffff"}
    result = QtGui.QPalette()
    for role, color in colors.items():
        result.setColor(getattr(QtGui.QPalette, role), QtGui.QColor(color))
    return result


def settle():
    for _ in range(5):
        QtWidgets.QApplication.processEvents()


@unittest.skipUnless(cv is not None, "PySide is not available")
class ChatViewTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.root = QtWidgets.QWidget()
        self.root.resize(380, 600)
        layout = QtWidgets.QVBoxLayout(self.root)
        self.chat = cv.ChatView()
        self.composer = cv.Composer()
        layout.addWidget(self.chat, 1)
        layout.addWidget(self.composer)
        self.root.show()
        settle()

    def tearDown(self):
        self.root.close()
        self.root.deleteLater()
        settle()

    def test_markdown_is_rendered_and_html_is_shown_as_text(self):
        message = self.chat.add_assistant("Use **bold** and <b>tags</b>")
        text = message.text()
        self.assertIn("bold", text)
        self.assertNotIn("**", text)
        self.assertIn("<b>tags</b>", text)

    def test_plain_text_lists_the_conversation(self):
        self.chat.add_user("Make a box")
        self.chat.add_assistant("Done")
        self.chat.add_notice("Stopped.", "warning")
        self.assertEqual(self.chat.plain_text(), "You: Make a box\n\nAssistant: Done\n\nStopped.")

    def test_sticks_to_the_bottom_unless_scrolled_up(self):
        for index in range(40):
            self.chat.add_assistant("Line {}".format(index))
        settle()
        bar = self.chat.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertTrue(self.chat.at_bottom())
        bar.setValue(0)
        settle()
        self.chat.add_assistant("Arrives while reading history")
        settle()
        self.assertEqual(bar.value(), 0)
        self.chat.add_user("New request")
        settle()
        self.assertTrue(self.chat.at_bottom())

    def test_step_cards_open_for_failures_and_review_only(self):
        done = self.chat.add_step("t/1")
        done.update_step("1", "Create box", "x = 1", "executed", "Changed: Box", can_undo=True)
        failed = self.chat.add_step("t/2")
        failed.update_step("2", "Fillet", "y = 2", "failed", "Rolled back")
        review = self.chat.add_step("t/3")
        review.update_step("3", "Cut", "z = 3", "review", "", can_run=True)
        settle()
        self.assertFalse(done.expanded)
        self.assertTrue(failed.expanded and review.expanded)
        self.assertTrue(review.run_button.isVisibleTo(review))
        self.assertTrue(done.undo_button.isVisibleTo(done))
        self.assertFalse(failed.run_button.isVisibleTo(failed))
        done.toggle()
        self.assertTrue(done.expanded)
        self.assertEqual(done.code_text(), "x = 1")
        self.assertEqual(done.details_text(), "Changed: Box")
        self.assertEqual(self.chat.step_cards(), [done, failed, review])
        failed.set_expanded(False)  # The user's choice wins over later updates.
        failed.update_step("2", "Fillet", "y = 2", "failed", "Rolled back")
        self.assertFalse(failed.expanded)

    def test_card_signals_and_copy(self):
        card = self.chat.add_step("t/1")
        card.update_step("1", "Cut", "doc.recompute()", "review", "", can_run=True, can_undo=True)
        runs, undos = [], []
        card.run_requested.connect(runs.append)
        card.undo_requested.connect(undos.append)
        card.run_button.click()
        card.undo_button.click()
        self.assertEqual((runs, undos), (["t/1"], ["t/1"]))
        card.copy_code()
        self.assertEqual(QtWidgets.QApplication.clipboard().text(), "doc.recompute()")

    def test_long_code_scrolls_inside_the_card(self):
        card = self.chat.add_step("t/1")
        card.update_step("1", "Long", "\n".join("line{}".format(i) for i in range(40)), "failed", "")
        settle()
        card._fit_code()
        metrics = QtGui.QFontMetrics(card.code.font())
        self.assertLess(card.code.height(), metrics.lineSpacing() * (cv.MAX_CODE_LINES + 3))

    def test_status_line(self):
        self.chat.set_status("Thinking…")
        self.assertEqual(self.chat.status_text(), "Thinking")
        self.chat.set_status(None)
        self.assertEqual(self.chat.status_text(), "")

    def test_empty_state_examples_fill_the_composer(self):
        chosen = []
        self.chat.example_chosen.connect(chosen.append)
        self.chat.show_empty_state(["Make a gear"], "Note")
        settle()
        chip = self.chat.empty.examples[0]
        position = QtCore.QPointF(5, 5)
        event = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonRelease, position,
                                  chip.mapToGlobal(position.toPoint()), QtCore.Qt.LeftButton,
                                  QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(chip, event)
        self.assertEqual(chosen, ["Make a gear"])
        self.chat.add_user("Make a gear")
        self.assertIsNone(self.chat.empty)

    def test_composer_grows_to_a_limit_and_toggles_send_and_stop(self):
        self.composer.set_text("one line")
        settle()
        single = self.composer.input.height()
        self.composer.set_text("\n".join("line {}".format(i) for i in range(30)))
        settle()
        tall = self.composer.input.height()
        metrics = QtGui.QFontMetrics(self.composer.input.font())
        self.assertGreater(tall, single)
        self.assertLessEqual(tall, metrics.lineSpacing() * (cv.MAX_INPUT_LINES + 2))
        sends, stops = [], []
        self.composer.send_clicked.connect(lambda: sends.append(1))
        self.composer.stop_clicked.connect(lambda: stops.append(1))
        self.composer.send_button.click()
        self.composer.set_running(True)
        self.composer.send_button.click()
        self.assertEqual((len(sends), len(stops)), (1, 1))
        self.composer.set_running(False)
        self.composer.set_text("")
        self.assertFalse(self.composer.send_button.isEnabled())

    def test_context_chip_and_mode(self):
        self.composer.context_chip.setChecked(False)
        self.assertTrue(self.composer.context_notice.isVisibleTo(self.composer))
        self.composer.auto_action.setChecked(False)
        self.assertTrue(self.composer.mode_chip.text().startswith("Review"))
        self.composer.auto_action.setChecked(True)
        self.assertTrue(self.composer.mode_chip.text().startswith("Auto"))

    def test_colors_follow_the_application_palette(self):
        original = QtWidgets.QApplication.palette()
        try:
            for dark in (False, True):
                QtWidgets.QApplication.setPalette(palette(dark))  # FreeCAD switching themes.
                settle()
                theme = self.chat.theme
                self.assertEqual(theme.dark, dark)
                for background in (theme.window, theme.bubble, theme.surface, theme.code):
                    self.assertGreaterEqual(cv.contrast(theme.text, background), 4.5)
                self.assertGreaterEqual(cv.contrast(theme.muted, theme.window), 3.0)
                self.assertGreaterEqual(cv.contrast(theme.accent_text, theme.accent), 3.0)
                self.assertEqual(self.composer.theme.dark, dark)
        finally:
            QtWidgets.QApplication.setPalette(original)

    def test_card_title_prefers_what_the_step_did(self):
        card = self.chat.add_step("t/1")
        card.update_step("1", "I'll create the box first", "x = 1", "executed", "",
                         summary="Created Box")
        self.assertEqual(card.full_title, "Step 1 · Created Box")
        card.update_step("1", "I'll create the box first", "x = 1", "failed", "")
        self.assertEqual(card.full_title, "Step 1 · I'll create the box first")


if __name__ == "__main__":
    unittest.main()
