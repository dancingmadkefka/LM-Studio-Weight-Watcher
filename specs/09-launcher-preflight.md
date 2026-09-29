# 9. The launcher runs a broken Python silently

Priority: high. Touches `run_watcher.bat`, `lmstudio_weight_watcher.py`, and the
README. This is the only failure in the review that has already happened on this
machine.

## Observed

`watcher-error.log` ends with:

```
File "C:\Users\daniel\VSCodeProjects\LM-Studio-Weight-Updater\lmstudio_weight_watcher.py", line 18, in <module>
    from PIL import Image, ImageDraw, ImageFont
  File "C:\Users\daniel\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages\PIL\Image.py", line 95, in <module>
    from . import _imaging as core
ImportError: cannot import name '_imaging' from 'PIL'
```

The watcher ran under a Python that has a broken Pillow. `run_watcher.bat:22-51`
selects the interpreter from `%LMSTUDIO_WATCHER_PYTHON%`, then `.venv\Scripts\
python.exe`, then `venv`, then `python`, then `py -3`. Any of those can be
missing a working Pillow, pystray, or windows-toasts, and the failure mode is a
traceback appended to a log file. Started from `run_watcher_hidden.vbs`, which
runs `cmd.exe /c ... --hidden-launch` with window state 0, the user sees
nothing at all: no window, no toast, no tray icon.

Second defect in the same file: the hidden-launch branches at lines 63-65
expand `%1 %2 %3 %4 %5 %6 %7 %8 %9`, so a tenth argument is silently dropped.
The interactive branch at line 69-71 uses `%*`. With `--hidden-launch` shifted
off, four options are dropped, which is enough to lose `--timeout-seconds` and
`--state-file`.

Related, and the likely source of the wrong interpreter: `.venv` in this repo
resolves its standard library to `C:\Users\daniel\miniforge3\Lib`, so it is a
venv built on a conda base that inherits conda site-packages. That is how a
broken `PIL` shadows the installed one.

## Required

1. Before any Tk or pystray work, the watcher MUST verify that its third-party
   imports are usable: `PIL.Image` (with `ImageDraw` and `ImageFont`), `pystray`,
   and `windows-toasts` when on Windows.
2. On failure, the watcher MUST report the problem in a way a hidden launch
   cannot swallow. A Windows message box is the right mechanism; use one
   without importing PIL to show it, so the check can report a broken PIL.
3. The report MUST name the running interpreter (`sys.executable`), the module
   or import that failed, and the exact remediation command for this project:
   `"<interpreter>" -m pip install -r requirements.txt`.
4. The check MUST run before `--once` mode and before the tray app starts. A
   scheduled `--once` run still needs the failure in the log, so in that mode
   print to stderr and give a distinct exit code rather than showing a modal
   box with no user present.
5. `run_watcher.bat` MUST forward every argument in the hidden branch. Use a
   single `%*` expansion. CMD does not change `%*` after `shift`, so the watcher
   MUST accept and ignore the `--hidden-launch` marker while preserving every
   user argument.
6. Interpreter selection MUST NOT be satisfied by an interpreter that cannot
   import the app's dependencies. If the preferred candidate fails the preflight
   and a later candidate exists, the launcher or the watcher MUST report which
   candidates were tried and which one failed.
7. The failure MUST be written to `watcher-error.log` as well, so the hidden
   path leaves evidence even when a message box cannot be shown.
8. The README MUST document the interpreter search order, the
   `LMSTUDIO_WATCHER_PYTHON` override, and the warning not to create the venv
   with `--system-site-packages` or on a conda base that inherits site-packages.

## Out of scope

- Rewriting the launcher in PowerShell. Keep the batch file.
- Auto-repair. Do not install packages on the user's behalf.

## Acceptance tests

Unit-testable parts go in `tests/test_lmstudio_weight_watcher.py`:

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_preflight_reports_broken_pillow` | patch the import probe to raise `ImportError` for `PIL._imaging` | result names the module, `sys.executable`, and the pip command |
| `test_preflight_passes_with_dependencies` | probe returns success | no report |
| `test_preflight_error_exit_code_is_distinct` | `--once` path with a failing preflight | non-zero exit that is not the generic 1 used for app crashes; message on stderr |
| `test_preflight_runs_before_window_creation` | spy on window creation | preflight called first |

The probe must be a seam: a module-level function that imports the list and
returns a structured result, patched by these tests. Do not call `importlib` on
the real `PIL` inside the test.

Batch file checks, skipped unless `os.name == "nt"`:

| Check | Assertion |
| --- | --- |
| `run_watcher.bat` hidden branch argument forwarding | a test that runs the batch file with a stub Python printing `sys.argv`, passing eleven arguments, sees all eleven |

If running the batch file under test is too fragile, assert on the file's
contents for `%*` in the hidden branch and record the manual check in the
commit message. Do not leave the truncation unverified either way.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_watcher -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Manual, required before closing:

1. From a console: `.\run_watcher.bat --once` using the current `.venv`. Expect
   a real check or a clear dependency report, not a traceback.
2. And with `LMSTUDIO_WATCHER_PYTHON` pointed at a Python without the
   dependencies: expect the message box naming that interpreter and the pip
   command.
3. And `wscript .\run_watcher_hidden.vbs` with the same broken override: expect
   a message box, and a line in `watcher-error.log`.
