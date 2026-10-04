#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Create the GitHub fork and apply this source overlay without overwriting work."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys

OVERLAY = Path(__file__).resolve().parents[1]
REGISTRATION = '''
# Optional GUI assistant; no additional Python dependencies.
option(BUILD_AI_ASSISTANT "Build the AI assistant panel" ON)
if(BUILD_GUI AND BUILD_AI_ASSISTANT)
  add_subdirectory(AIAssistant)
endif()
'''


def command(args, cwd=None, capture=False):
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def apply_overlay(checkout):
    checkout = Path(checkout).resolve()
    cmake = checkout / "src/Mod/CMakeLists.txt"
    if not cmake.is_file() or not (checkout / "src/Gui").is_dir():
        raise ValueError("Target must be a complete FreeCAD source checkout.")
    module = checkout / "src/Mod/AIAssistant"
    if module.exists():
        raise ValueError("AIAssistant already exists; refusing to overwrite it.")
    contents = cmake.read_text()
    if "BUILD_AI_ASSISTANT" in contents or re.search(r"add_subdirectory\s*\(\s*AIAssistant", contents):
        raise ValueError("Assistant CMake registration already exists; refusing a duplicate.")
    # Check every destination before making any changes.
    extras = [Path("README.AI_ASSISTANT.md"), Path("tests/ai_assistant"),
              Path("tools/fork_with_ai.py")]
    for relative in extras:
        if (checkout / relative).exists():
            raise ValueError("Destination already exists: " + str(relative))
    shutil.copytree(OVERLAY / "src/Mod/AIAssistant", module,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cmake.write_text(contents.rstrip() + "\n" + REGISTRATION)
    for relative in extras:
        source, destination = OVERLAY / relative, checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=OVERLAY.parent / "FreeCAD-fork",
                        help="New clone directory, or existing checkout with --apply-only")
    parser.add_argument("--apply-only", action="store_true", help="Apply to an existing source checkout")
    parser.add_argument("--publish", action="store_true", help="Push the tested ai-assistant branch to your fork")
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    if args.apply_only:
        if args.publish:
            parser.error("--publish cannot be combined with --apply-only")
        apply_overlay(checkout)
        print("Applied the assistant to " + str(checkout))
        return
    if checkout.exists():
        raise ValueError("Clone directory already exists; choose a new --checkout path.")
    for executable in ("gh", "git"):
        if shutil.which(executable) is None:
            raise ValueError(executable + " must be installed first.")
    command(["gh", "auth", "status"])
    owner = command(["gh", "api", "user", "--jq", ".login"], capture=True)
    if not re.fullmatch(r"[a-zA-Z0-9-]+", owner):
        raise ValueError("Could not determine your GitHub login.")
    command(["gh", "repo", "fork", "FreeCAD/FreeCAD", "--clone=false"])
    remote = "https://github.com/" + owner + "/FreeCAD.git"
    command(["git", "clone", "--filter=blob:none", remote, str(checkout)])
    command(["git", "remote", "add", "upstream", "https://github.com/FreeCAD/FreeCAD.git"], checkout)
    command(["git", "switch", "-c", "ai-assistant"], checkout)
    apply_overlay(checkout)
    command([sys.executable, "-m", "unittest", "discover", "-s", "tests/ai_assistant", "-v"], checkout)
    command(["git", "add", "src/Mod/CMakeLists.txt", "src/Mod/AIAssistant",
             "tests/ai_assistant", "tools/fork_with_ai.py", "README.AI_ASSISTANT.md"], checkout)
    command(["git", "diff", "--cached", "--check"], checkout)
    command(["git", "commit", "-m", "Add autonomous AI modeling assistant",
             "-m", "Assisted-by: GPT-6 (Codex)"], checkout)
    if args.publish:
        command(["git", "push", "-u", "origin", "ai-assistant"], checkout)
        print("Published: https://github.com/" + owner + "/FreeCAD/tree/ai-assistant")
    else:
        print("Prepared and committed locally. Publish with: git -C " + str(checkout)
              + " push -u origin ai-assistant")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("Fork setup failed: " + str(error), file=sys.stderr)
        sys.exit(1)
