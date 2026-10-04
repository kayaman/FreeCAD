# FreeCAD AI Assistant

This module adds **AI → AI Assistant**, a dockable autonomous modeling assistant.
It embeds the signed-in **Claude Code**, **Codex**, or **GitHub Copilot** CLI,
following Bancada's native assistant approach. There are no API URL, API key,
or token fields. Authentication stays with each official CLI.

The assistant creates and edits parametric features in the active document. It
validates each step against the document's existing errors and returns results
to the provider, which plans the next step or repairs errors. Python print output
comes back as bounded feedback, so the agent can query computed geometry before
it acts.

Automatic steps run in a separate headless FreeCAD, so **Stop** works at any
time. Each run allows six code steps; **Continue** grants another run of the same
task. When no document is open, the first modeling step creates an `AIModel`
document.

## Current delivery

The assistant is published on the `ai-assistant` branch of
[kayaman/FreeCAD](https://github.com/kayaman/FreeCAD/tree/ai-assistant), a fork of
upstream FreeCAD. Each release attaches `AIAssistant-<version>.zip` for installing
into an existing FreeCAD (see below). No upstream pull request has been opened.

The module is developed as a source overlay. `tools/fork_with_ai.py` applies its
CMake registration to an actual checkout without replacing other CMake entries.

Create your fork, apply the assistant on an `ai-assistant` branch, test, commit
and publish it from a shell with GitHub connectivity:

```sh
python3 FreeCAD/tools/fork_with_ai.py --publish
```

The script uses the account signed into `gh`, preserves upstream source and
history, creates an `upstream` remote, and refuses to overwrite existing files.
Without `--publish`, it stops after committing locally. If you already have
a full source checkout:

```sh
python3 FreeCAD/tools/fork_with_ai.py --apply-only --checkout /path/to/FreeCAD
```

## Use in an existing FreeCAD installation

Unzip a release's `AIAssistant-<version>.zip`, or copy the entire
`src/Mod/AIAssistant` directory, into the user `Mod` directory
reported by `App.getUserAppDataDir()` in FreeCAD's Python console, then restart
FreeCAD. This does not require recompiling FreeCAD. The layout must be:

```text
<user app data>/Mod/AIAssistant/InitGui.py
<user app data>/Mod/AIAssistant/freecad_ai/...
```

In a full source build the module is enabled by default when `BUILD_GUI` is on.
Disable it with `-DBUILD_AI_ASSISTANT=OFF`. The build and install CMake rules
copy every runtime module, including the worker script, into `Mod/AIAssistant`.
The module uses Qt's asynchronous subprocess API, Python's standard library and
the `FreeCADCmd` that ships with FreeCAD. Install the provider's official CLI
separately; no additional Python packages are needed.

## Native account integration

Sign in once using the official provider CLI, then open
**AI → AI Assistant → Settings** and select the provider:

| Provider | Native sign-in | Status check without inference | Embedded process |
| --- | --- | --- | --- |
| Claude Code | `claude auth login` with your Claude account | `claude auth status --json` | Claude's `stream-json` stdin/stdout protocol, as used by Bancada |
| Codex | `codex login` with ChatGPT | `codex login status` | `codex exec --json`, consuming native turn and item events |
| GitHub Copilot | `copilot login` | none (the CLI has no status command) | Copilot's official programmatic CLI using its saved login |

### Readiness

The panel header shows the provider's verified state:

- **checking**
- **ready**
- **CLI not found**
- **sign-in required** (also shown when an API-billed login is detected)
- **unsupported version**
- **unknown** (Copilot, or output the assistant cannot read)
- **unavailable**

A check runs without any model request when the panel opens and after settings
change. It confirms the CLI starts and its version is at least the oldest
version verified with the assistant. Where the CLI offers one, it also runs the
native status command.

Settings has two buttons:

- **Check connection** repeats that check for the values in the dialog.
- **Live check** sends one short chat-only request through your account. It
  never runs automatically.

Successful checks are cached for five minutes per provider and executable; the
cache is reset when the CLI binary changes. Settings changes and authentication
failures invalidate it. While a sign-in is cached, requests skip the separate
login check.

Failures are classified from the CLI's output as:

- authentication
- unsupported model
- rate or usage limit
- timeout
- protocol

When the evidence is uncertain, the message stays generic. Raw stderr and
credentials are never shown.

Commands and flags were verified on 2026-10-03 against the installed CLIs' own
`--help`: Claude Code 2.1.289, codex-cli 0.160.0, Copilot CLI 1.0.91. Older
versions are reported as unsupported rather than assumed to work. See the
official [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference),
[Codex non-interactive mode](https://developers.openai.com/codex/noninteractive),
and [Copilot programmatic CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference).

The model field is optional: leave it blank to use the CLI's default. The
assistant discovers CLI executables on PATH and in common user install
directories. When Node is absent in a desktop or Flatpak environment, it uses
the official native binaries bundled with the Codex/Copilot npm packages.

FreeCAD never reads credential files, extracts OAuth tokens, requests keys, or
stores credentials. It removes API-key and token environment overrides from the
child processes so they use their saved account login, and refuses API-billed
login modes. Only provider, optional model, CLI path, and timeout are stored in
FreeCAD preferences. Changing providers starts a new chat. Interactive sign-in
and inference are never triggered at startup.

Claude and Copilot run with their own tools disabled; Codex uses a read-only
sandbox. Claude session persistence is disabled and Codex uses ephemeral
sessions; Copilot may save conversations according to its own settings.
Personal MCP servers are excluded.

### Process supervision and Stop

Every provider and worker process is supervised as a whole process tree.

- **On Linux and macOS**, each process starts in its own session. Stop asks the
  whole process group to end, then forces it after 500 ms, without blocking the
  GUI. Other sessions of the same CLI are never touched.
- **On Windows**, the tree is ended by process ID with `taskkill /T`. This is
  implemented but untested.

Temporary directories outlive every owned process and are removed on
completion, timeout, cancellation, startup failure and application exit.

## Modeling

Describe the model and include required dimensions, for example:

> Create a parametric box 40 × 30 × 10 mm. Add a centered 5 mm cylindrical hole
> through the 10 mm thickness.

### Automatic steps

**Run modeling steps automatically** is enabled by default. Each step runs like
this:

1. FreeCAD copies the document, including unsaved edits, to a private temporary
   file. The original's file name and modified state are unchanged.
2. `FreeCADCmd --safe-mode` runs the step on that copy and recomputes it.
3. FreeCAD applies the resulting property values to your document in one
   undoable transaction named `AI assistant <task>/<n>`. This is the short
   "Applying changes" phase, and it does not recompute.

Sketches, constraints, expressions, PartDesign features, internal links, object
identity and appearance are preserved; the result is not a flattened shape.

Stop ends the worker at any point. A Stop during the brief apply phase prevents
the next step; an apply that has started finishes.

The step is discarded, with your document untouched, if any of these happen:

- the task is stopped, paused or replaced
- you edit the model while the step is being computed
- the worker crashes or times out

Some steps cannot be transferred. Automatic mode never falls back to running
them inside FreeCAD. Instead it stops, explains why, and offers **Run reviewed
code** for that step. These steps include:

- documents linked to other documents
- steps that need the GUI (`Gui`, view settings such as colors set in code)
- custom Python features
- type changes

See `src/Mod/AIAssistant/WORKER_NOTE.md` for the transfer method, its limits and
measurements.

### Reviewed mode

Turn automatic steps off to inspect each step's code and click **Run reviewed
code**. Reviewed code runs inside FreeCAD after you have seen it and cannot be
stopped midway.

### Validation against existing errors

Before a step, the assistant records which features are already invalid. After
recompute, a step fails and is rolled back if it does any of the following:

- introduces a newly invalid feature
- worsens another feature's error
- leaves a feature it changed still invalid (repair progress cannot be verified)
- fails to recompute
- produces no solid, or an invalid one, for a solid-producing feature it created
  or edited

Repairs that improve the document pass. Errors that already existed and were not
touched are reported separately and do not block unrelated edits; the provider
is told they were not caused by its step. Rollback restores every value and
shape, and the original error state.

### Task timeline, Continue and Undo

The **Task timeline** lists every step of the document's tasks with its state:

- proposed
- executed
- failed
- cancelled
- rejected
- undone

The Code and Result tabs show the step's code, the changed objects (and, for
automatic steps, changed properties and expressions), the result and
diagnostics. While the provider responds, a status line shows progress instead
of partial output.

After six executions the task pauses. **Continue** grants another six for the
same task, with its requirements and ledger. A step that already ran is never
repeated.

**Undo last step** and **Undo task** work only when the assistant's transactions
are the current, uninterrupted top of the document's undo history, nothing
changed since the last step, and FreeCAD still holds every transaction involved.
Otherwise nothing changes and the panel points you to FreeCAD's normal Undo. A
whole snapshot is never restored over later edits. Steps that failed and were
rolled back have nothing to undo. Undone steps are recorded and the provider is
told.

### Model context

**Send model summary and selection** is enabled by default. It shares a bounded
summary of the active document, ordered as follows:

1. selected objects
2. their owning Bodies and Parts
3. their dependencies
4. everything else

Each object carries editable numeric, boolean and enumeration values with units
(mm, deg, mm², mm³; angles and volumes are never labelled mm). Selected objects,
their owners and their dependencies also get:

- placement
- bounding box
- dependency names
- sketch constraints with names and values
- for selected faces, edges and vertices: type, area or length, radius, normal

The summary is limited to:

- 80 objects
- 24 properties per object (48 for selected objects and their owners)
- 60 KB in total
- 40 selected objects and 12 subelements each

An `omitted` record says what was left out, so absence is not mistaken for an
empty model. If the selection itself does not fit, the panel asks you to select
less instead of sending a partial selection. Free text, file paths, raw shapes
and screenshots are never sent automatically.

Turning the checkbox off stops sending new summaries. Messages and summaries
already sent remain part of the chat; **New chat** starts clean.

### Design memory and session lifetime

Each open document has its own session. Switching documents pauses running work
and shows that document's chat; **Continue** resumes. Closing a document retires
its session; reopening the file starts a new one. Results that arrive after
Stop, New chat, a provider change, a document switch or a close can never modify
a model.

Within a session, these are kept apart from the recent-message window, which
holds 16 exchanges:

- your requests, pinned verbatim as requirements with their source message IDs
- an editable design brief
- the current goal
- an execution ledger of what actually ran

Requirements and the brief are sent with every request, so an early "2 mm walls"
is not forgotten. Proposals that never ran are marked so. The assistant's own
output is never pinned as a requirement.

Open **Design brief…** to edit the brief, mark a requirement as superseded by a
later one, or remove one; provenance is kept. When active requirements conflict
without an explicit override, the provider is told to ask. Pinned requirements
have a 32,000-character budget. When it is full, the request is not sent and the
brief opens with a summary for you to edit and approve; nothing is dropped
silently. Memory is local and in memory only; **New chat** clears it.

### Permissions

Generated code runs with **your user's Python permissions**, in the worker and
in reviewed mode. The worker isolates crashes and cancellation; it is **not a
security sandbox**. Transactions cannot undo filesystem or network effects, or
other Python side effects. The prompt tells the provider to operate only on the
target document, but that is not a security boundary. Inspect generated geometry
before use.

## Validation

Dependency-free tests (protocol, sessions, memory, context ordering, validation
rules, providers, Undo planning, overlay packaging):

```sh
python3 -m unittest discover -s FreeCAD/tests/ai_assistant -v
```

In a full fork checkout, omit the leading `FreeCAD/`. The FreeCAD integration
checks run from FreeCAD's GUI Python console through one entry point:

```python
import importlib.util
spec = importlib.util.spec_from_file_location("ai_integration", "/absolute/path/to/tests/ai_assistant/integration.py")
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)
integration.run()  # or run(["gui_smoke", "transport_smoke", "worker_smoke", "timeline_smoke"])
```

It uses fake providers and disposable documents only. It restores the original
active document and selection, and fails if any temporary directory or child
process is left behind.

- `gui_smoke`
  - late responses after every interruption
  - pause and resume
  - validation cases
  - real-geometry context
  - design memory
  - readiness display
- `transport_smoke`
  - real Qt subprocesses
  - process-tree Stop, timeout and shutdown with a GUI heartbeat
  - readiness states of fake signed-in, signed-out, API-billed, missing, obsolete
    and malformed CLIs
  - caching
  - failure classification
- `worker_smoke`
  - the worker feasibility fixture, timings, cancellation, crashes and validation
  - through the panel: Stop, crash, slow geometry, manual-edit rejection,
    reviewed-mode offer, Stop during apply
- `timeline_smoke`
  - timeline inspection and full task Undo
  - manual edits, outside Undo/Redo, missing history, a failed middle step
  - Stop, document switching, Continue

`tests/ai_assistant/live_native.py` is explicitly opt-in and uses your account
plan allowance.

- `run("claude" | "codex" | "copilot")` makes one chat-only request.
- `run_modeling("claude")` runs one real modeling task in a disposable document.

### Tested matrix (2026-10-03)

| Platform | Provider | Non-inference readiness | Live request | Live modeling task |
| --- | --- | --- | --- | --- |
| Linux, FreeCAD 1.1.4 Flatpak, PySide6 6.11.2 | Claude Code 2.1.289 (Claude account) | ready | passed | passed (2 worker steps, 20 s) |
| Linux, FreeCAD 1.1.4 Flatpak | Codex 0.160.0 (ChatGPT) | ready | passed | not run |
| Linux, FreeCAD 1.1.4 Flatpak | Copilot CLI 1.0.91 | unknown (no status command) | failed: "no authentication information found" inside the Flatpak sandbox | not run |
| Windows, macOS | all | untested | untested | untested |

The unit suite (107 tests) and all four integration checks pass in that
environment.

Copilot keeps its login in the system credential store, which the FreeCAD
Flatpak may not be able to reach. A host-side probe got past authentication but
was not completed. Copilot inside the Flatpak is therefore untested.

Process supervision and worker discovery have not been exercised on Windows or
macOS. A build and install from a complete FreeCAD source checkout has not been
validated.

The additions use `LGPL-2.1-or-later`, matching FreeCAD. They were created with
AI assistance (GPT-6 / Codex for the initial module; Claude Opus 5.5 / Claude
Code for the AI_ASSISTANT_PLAN work); no upstream pull request has been
submitted.
