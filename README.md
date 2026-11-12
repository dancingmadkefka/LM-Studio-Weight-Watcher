# LM Studio Weight Watcher

LM Studio doesn't tell you when a model you installed has changed on Hugging
Face. This tray app checks for you and can download the update. It works with
plain GGUFs, sharded weights, nested repo paths, and vision projectors.

## Setup

Windows, LM Studio with its `lms` CLI on PATH, Python 3.10 or newer.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_watcher.bat
```

To start at login:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

## Daily use

The tray icon shows the state: a green check when everything matches, a red
count when updates wait, amber when files were only removed upstream, blue
dots while it works. When a check finds something new, Windows shows a
notification. Click the tray icon to open the window.

The window lists every model that needs attention. Click one to see where it
comes from, what changed upstream, and where it lives on disk. Filters at the
top split the list into Needs attention, Snoozed, Dismissed, and Can't check.

The buttons at the bottom act on the selected models, or on everything pending
when nothing is selected:

- **Update** opens a confirmation that lists the models, files, download
  size, free disk space and upstream commit titles. Nothing downloads before
  you confirm. A progress bar with speed and time left replaces it, and you
  can cancel until installation starts.
- **Snooze** hides models for 4 hours.
- **Dismiss** hides models until their files change upstream again.

Snooze and Dismiss can be undone from the status line. Right-click a model for
more: move it back to Needs attention, open it on Hugging Face, show the file
in Explorer, copy its details.

If LM Studio still has a file open, the update stops and asks you to unload
the model; Try again picks it up. If a file was deleted upstream, the details
list the quants the repository still offers.

Keyboard: F5 checks now, Enter updates, Delete dismisses, Esc clears the
selection or hides the window. The window follows the Windows light or dark
setting; set `LMSTUDIO_WATCHER_THEME=light` or `dark` to override it.

## Scripts

| Script | Use |
| --- | --- |
| `lmstudio_weight_watcher.py` | The tray app |
| `lmstudio_weight_checker.py` | Check only, print a report; `--json` for scripts |
| `lmstudio_weight_updater.py` | Download and install updates from the CLI |

```powershell
# Show the plan as JSON; download nothing
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --dry-run

# Update every pending model with an available update
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --yes

# Update specific models (keys as printed by the checker)
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --model-key "publisher/model"
```

All three accept `--models-root`, `--state-file`, and `--timeout-seconds`.
`--help` on any script lists the rest.
The updater's `--dry-run` always prints a JSON plan, with or without `--json`.
For a normal update, `--json` prints the result as JSON instead of a status line.

## What happens during an update

1. Files download into `.weight-watcher-staging` next to the models root, on
   the same volume so installs are atomic moves. Partial downloads resume.
2. Each staged file is checked against the remote size and SHA-256 before
   anything is replaced.
3. Existing files are renamed to `.lmww-backup-<job>`, the staged files move
   into place, and the checker confirms the models are up to date.
4. Rollback copies remain until a later clean check. `--keep-backups` retains
   them indefinitely.
5. On failure the originals are restored. An install interrupted by a crash is
   detected on the next start and rolled back; if the state is ambiguous, both
   files are left in place and reported.
6. Cleanup only deletes files recorded as updater backups or staging files.

`huggingface_hub` is required for downloads. `hf_xet` is recommended for speed;
when it is missing or disabled, the updater reports once per job that downloads
will use the slower HTTP path and continues the update.

Before deleting a backup, cleanup flushes its directory on systems that allow
it. Windows has no directory fsync; on NTFS, cleanup relies on metadata
ordering.

Before downloading, the updater checks free space on the models volume. The
estimate includes remaining downloads, space for backups, and a reserve at
least as large as the biggest selected file. Staging on another volume is
rejected.

## Notes

- Public repos need no token. Gated and private models use normal Hugging Face
  CLI login; tokens are redacted from state and error reports.
- State lives in `%APPDATA%\LM Studio Weight Watcher\state.json`. Older formats
  and locations are migrated.
- The launcher looks for Python at `%LMSTUDIO_WATCHER_PYTHON%`, then `.venv`,
  `venv`, `python`, then `py -3`. Set `LMSTUDIO_WATCHER_PYTHON` to a full
  `python.exe` path to force one interpreter. The launcher checks each candidate's
  dependencies before starting and records failures in `watcher-error.log` next
  to the launcher. A hidden tray launch also shows a Windows error box if no
  usable interpreter is found.
- Create `.venv` without `--system-site-packages`. Avoid a conda base that
  inherits site-packages; a broken package there can override the copy installed
  in `.venv`. To repair the selected interpreter, run
  `"<interpreter>" -m pip install -r requirements.txt` from this folder.

## Tests

The suite uses fake downloaders and temporary directories; no network, no
changes to your models.

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## License

[The Unlicense](LICENSE).
