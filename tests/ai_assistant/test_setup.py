# SPDX-License-Identifier: LGPL-2.1-or-later
import importlib.util
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fork_setup", SOURCE / "tools/fork_with_ai.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def test_overlay_preserves_existing_cmake(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src/Mod").mkdir(parents=True)
            (target / "src/Gui").mkdir()
            cmake = target / "src/Mod/CMakeLists.txt"
            cmake.write_text("# Existing build\nadd_subdirectory(Part)\n")
            setup.apply_overlay(target)
            self.assertIn("add_subdirectory(Part)", cmake.read_text())
            self.assertEqual(cmake.read_text().count("add_subdirectory(AIAssistant)"), 1)
            self.assertTrue((target / "src/Mod/AIAssistant/InitGui.py").is_file())
            self.assertTrue((target / "src/Mod/AIAssistant/freecad_ai/gui.py").is_file())
            self.assertTrue((target / "README.AI_ASSISTANT.md").is_file())
            self.assertFalse(list(target.rglob("__pycache__")))
            before = cmake.read_text()
            with self.assertRaises(ValueError):
                setup.apply_overlay(target)
            self.assertEqual(cmake.read_text(), before)

    def test_every_runtime_module_is_built_installed_and_copied(self):
        module = SOURCE / "src/Mod/AIAssistant"
        runtime = sorted(path.relative_to(module).as_posix()
                         for path in (module / "freecad_ai").glob("*.py"))
        self.assertIn("freecad_ai/worker.py", runtime)
        cmake = (module / "CMakeLists.txt").read_text()
        scripts = cmake.split("set(AIAssistant_Scripts", 1)[1].split(")", 1)[0]
        installed = cmake.split("install(FILES\n", 1)[1].split("DESTINATION", 1)[0]
        for relative in runtime:
            self.assertIn(relative, scripts, relative + " missing from the build copy list")
            self.assertIn(relative, installed, relative + " missing from the install list")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src/Mod").mkdir(parents=True)
            (target / "src/Gui").mkdir()
            (target / "src/Mod/CMakeLists.txt").write_text("# Existing build\n")
            setup.apply_overlay(target)
            for relative in runtime + ["WORKER_NOTE.md", "CMakeLists.txt"]:
                self.assertTrue((target / "src/Mod/AIAssistant" / relative).is_file(), relative)
            for smoke in ("integration.py", "gui_smoke.py", "transport_smoke.py",
                          "worker_smoke.py", "timeline_smoke.py"):
                self.assertTrue((target / "tests/ai_assistant" / smoke).is_file(), smoke)
            self.assertFalse((target / "AI_ASSISTANT_PLAN.md").exists())

    def test_rejects_incomplete_checkouts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                setup.apply_overlay(Path(directory))

    def test_conflicting_files_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src/Mod").mkdir(parents=True)
            (target / "src/Gui").mkdir()
            cmake = target / "src/Mod/CMakeLists.txt"
            cmake.write_text("# Existing build\n")
            readme = target / "README.AI_ASSISTANT.md"
            readme.write_text("Existing work")
            with self.assertRaises(ValueError):
                setup.apply_overlay(target)
            self.assertEqual(readme.read_text(), "Existing work")
            self.assertEqual(cmake.read_text(), "# Existing build\n")
            self.assertFalse((target / "src/Mod/AIAssistant").exists())
