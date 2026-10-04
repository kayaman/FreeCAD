# AI assistant modeling worker: feasibility result

Status: **gate passed** on FreeCAD 1.1.4 (Flatpak, Linux x86_64, PySide6 6.11.2),
2026-10-03. Evidence: `tests/ai_assistant/worker_smoke.py`, run inside the FreeCAD
GUI. Windows and macOS have not been exercised.

## Transfer method

1. **Discovery.** `FreeCADCmd` next to the running FreeCAD (`App.getHomePath()/bin`,
   which is `/app/freecad/bin` in the Flatpak), then next to `sys.executable`, then
   `PATH`. A candidate is used only if it reports the same FreeCAD version and can
   import Part, Sketcher and PartDesign. The result is cached for the session.
   The worker runs with `--safe-mode`, so user addons and macros do not load.
2. **Snapshot.** `Document.saveCopy()` writes the document, including unsaved
   edits, to a private temporary directory. The original document's `FileName`
   and modified flag are unchanged.
3. **Worker.** `FreeCADCmd --safe-mode worker.py`, started with
   `FREECAD_AI_WORKER=1`, because FreeCADCmd does not run scripts as `__main__`.
   The request (protocol version, snapshot path, code) arrives as JSON on stdin.
   The worker opens the copy, executes the step with `Gui`/`FreeCADGui` replaced by
   a stub that reports GUI use as unsupported, and recomputes.
   - The result is framed JSON on stdout, between sentinels, because FreeCADCmd
     prints banners. The size limit is 192 MB.
   - The result holds a delta: removed objects, added objects (name and TypeId,
     in document order), new dynamic properties, changed property contents
     (`dumpPropertyContent`, base64), each changed object's expressions, and the
     objects still touched or invalid after recompute.
4. **Apply (GUI thread, one transaction, no recompute).**
   1. Remove objects.
   2. `addObject` with the same names. A name clash aborts the step.
   3. Add dynamic properties.
   4. `restorePropertyContent` for non-shape properties.
   5. `setExpression` for expressions.
   6. Restore shape properties last.
   7. `purgeTouched()` on everything the worker left up to date.

### Findings that shaped the method

- **Expressions** do not survive `dumpPropertyContent`/`restorePropertyContent`;
  they travel as `(path, expression)` pairs.
- **ZIP timestamps.** `dumpPropertyContent` returns a ZIP whose headers include a
  2-second timestamp. Digests use the archive members, not the bytes. The same
  bug made the original document fingerprint report spurious changes.
- **Element maps.** Saving a document (including `saveCopy`) changes how shapes
  serialize. Shape properties are therefore digested by
  `exportBrepToString()`, which is stable across saving and rendering, detects
  geometric change, and is restored exactly by Undo.
- **Dependents.** A dependent `App::Link` was left touched by the restore until
  the worker's post-recompute touched set was used to purge.

## Verified

The fixture (`worker_smoke.py`) is a PartDesign Body with a constrained sketch
(named `width`/`height` constraints) and a Pad, plus an `App::Link` to the Body.
The Pad has a custom appearance. The document is saved, then left with unsaved
edits.

The step changes a sketch datum, adds an expression (`Pad.Length =
Sketch.Constraints.height / 2`), and adds a sketch with a named radius constraint
and a through-all Pocket. After applying:

- Object identity is preserved (the same `Pad` object).
- Body Group, Tip, Pocket Profile and BaseFeature are correct.
- The expression and constraints are present.
- The appearance is kept, and the link still points at the Body.
- Nothing is touched or invalid.
- Undo restores the exact pre-step fingerprint, and Redo restores the exact
  post-step fingerprint.
- Changing the sketch height afterwards re-drives the Pad and Pocket. The model
  stays parametric, not a flattened shape.

Also verified:

- An infinite loop in the worker is cancelled in about 0.4 s, and the document is unchanged.
- A worker crash (`os.abort()`) is reported, and the document is unchanged.
- GUI-dependent code is reported as unsupported.

Types exercised: `PartDesign::Body`, `PartDesign::Pad`, `PartDesign::Pocket`,
`Sketcher::SketchObject`, `App::Link`, `Part::Box`, `Part::Feature`.

## Measured

| Case | Snapshot | Worker | GUI apply | Max GUI heartbeat gap (50 ms timer) |
| --- | --- | --- | --- | --- |
| Pocket fixture | 0.006 s | 0.40 s | 0.020 s | 0.127 s |
| 20 fused spheres (26 faces, 0.01 MB delta) | 0.138 s | 0.30 s | 0.013 s | 0.061 s |
| 120 fused spheres (126 faces, 0.03 MB) | 0.008 s | 1.22 s | 0.030 s | 0.083 s |
| 400 fused spheres (406 faces, 0.08 MB) | 0.019 s | 10.16 s | 0.086 s | 0.062 s |

Expensive geometry and recomputation stay in the worker. The GUI phase only
deserializes results. Its cost grows with the size of the changed shapes; it
does not depend on how hard they were to compute.

## Limits and unsupported operations

- **Custom Python features** (objects with a `Proxy`) cannot be recreated from
  property content; the step is rejected as unsupported.
- **GUI use** (`Gui`, `FreeCADGui`, view-provider properties such as colors set
  in code) is unsupported in the worker. New objects get default view providers.
  PartDesign's GUI-only habit of hiding a consumed sketch does not happen.
- **External links** to other documents: not part of the copy; treated as
  unsupported by the production capability check.
- **Object type changes** (same name, different TypeId) abort the step.
- A name clash on apply aborts the step inside its transaction.
- The worker isolates crashes and cancellation; it is **not a security sandbox**.
  Generated Python runs with the user's permissions.
- Types beyond those listed under "Verified" are transferred by the same
  generic property mechanism but are not individually verified.

## Production integration

Automatic steps in the panel use this worker.

- **Capability checks.** These stop the step before or after the worker runs,
  explain why, and offer reviewed mode for that step. Automatic mode never falls
  back to running code inside FreeCAD.
  - documents that link to other documents
  - GUI use
  - custom Python features
  - object type changes
  - a missing compatible FreeCADCmd
- **Stale results.** A worker result is accepted only while its task, request and
  state still match. It is applied only if the document's fingerprint still
  equals the snapshot's. A manual edit during the run discards the candidate.
- **Applying.** Each applied step is one transaction named
  `AI assistant <task>/<n>`, shown as "Applying changes". Applying is short and
  cannot be interrupted midway. A Stop pressed meanwhile prevents the next step.
- **Reviewed code** still runs inside FreeCAD, after the user has seen it, and
  cannot be stopped midway.

Verified through the panel in `worker_smoke.py`:

- Stop ends a looping worker with the GUI responsive.
- A crash is reported to the provider as a failed step.
- Slow geometry is applied with the GUI responsive.
- A manual edit rejects the candidate.
- A GUI-dependent step offers reviewed mode.
- Stop during apply prevents the next step.
