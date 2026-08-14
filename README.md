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

When a check finds changes, the alerts window opens with one row per model.
Update Selected and Update All Pending show a confirmation first: which models
and files, how many bytes, free space, upstream commit titles. Downloads can be
cancelled until the final swap. If LM Studio still has a file open, the row
shows "Waiting for model unload"; unload the model and retry.

If a file was deleted upstream, the alert lists the quants that still exist in
the repo.

Alerts can be acknowledged or snoozed.

## Scripts

| Script | Use |
| --- | --- |
| `lmstudio_weight_watcher.py` | The tray app |
| `lmstudio_weight_checker.py` | Check only, print a report; `--json` for scripts |
| `lmstudio_weight_updater.py` | Download and install updates from the CLI |

```powershell
# Show what would happen, download nothing
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --dry-run

# Update every pending model with an available update
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --yes

# Update specific models (keys as printed by the checker)
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --model-key "publisher/model"
```

All three accept `--models-root`, `--state-file`, and `--timeout-seconds`.
`--help` on any script lists the rest.

## What happens during an update

1. Files download into `.weight-watcher-staging` next to the models root, on
   the same volume so installs are atomic moves. Partial downloads resume.
2. Each staged file is checked against the remote size and SHA-256 before
   anything is replaced.
3. Existing files are renamed to `.lmww-backup-<job>`, the staged files move
   into place, and the checker confirms the models are up to date.
4. On failure the originals are restored. An install interrupted by a crash is
   detected on the next start and rolled back; if the state is ambiguous, both
   files are left in place and reported.
5. Cleanup only deletes files the updater created.

## Notes

- Public repos need no token. Gated and private models use normal Hugging Face
  CLI login; tokens are redacted from state and error reports.
- State lives in `%APPDATA%\LM Studio Weight Watcher\state.json`. Older formats
  and locations are migrated.
- The launcher looks for Python at `%LMSTUDIO_WATCHER_PYTHON%`, then `.venv`,
  `venv`, `python`, then `py -3`. Problems end up in `watcher-launch.log` and
  `watcher-error.log` next to the launcher.

## Tests

The suite uses fake downloaders and temporary directories; no network, no
changes to your models.

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## License

[The Unlicense](LICENSE).
