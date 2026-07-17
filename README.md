# LM Studio Weight Watcher

LM Studio Weight Watcher checks models already installed in LM Studio, alerts when
their Hugging Face files change, and can safely download and install verified
updates. It supports ordinary GGUF files, sharded weights, nested repository paths,
and vision projectors discovered from LM Studio's inventory.

The updater stages every changed artifact, verifies its exact size and SHA-256 LFS
identity, and only then replaces installed files as one transaction. A failure rolls
the transaction back.

## Requirements and setup

- Windows with LM Studio and its `lms` CLI available.
- Python 3.10 or newer.
- Enough free space on the models volume for the remaining download plus a safety
  reserve. Staging deliberately stays on that volume so final replacements are
  atomic.

From PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`huggingface_hub`, `hf_xet`, and `tqdm` provide resumable Hugging Face downloads and
progress reporting. Public repositories need no token. For gated/private models,
authenticate using the normal Hugging Face CLI or environment configuration; tokens
and signed URL parameters are redacted from updater state and error reports.

## Tray watcher

Start the app with:

```powershell
.\run_watcher.bat
```

Or run it directly:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_watcher.py
```

The alerts window offers **Update Selected**, **Update All Pending**, and
**Cancel Update**. Before downloading, the confirmation shows the affected models
and files, remaining/total bytes, available space, and upstream commit titles.
Planning, downloading, verification, recovery, and installation run outside the Tk
UI thread.

Cancellation is honored during planning, downloading, and verification. Once the
verified installation transaction starts, cancellation is disabled and closing the
app waits for installation or rollback to finish safely. If LM Studio has a target
file open, the row displays **Waiting for model unload**; unload that model and retry.

The existing acknowledge and snooze actions still control alert reminders. They do
not discard update metadata.

### Watcher options

Run one check and update persistent alert state:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_watcher.py --once
```

Change scheduling, timeout, or models root:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_watcher.py `
  --check-interval-hours 4 `
  --reminder-interval-minutes 30 `
  --timeout-seconds 45 `
  --models-root "D:\LM Studio models\.cache\lm-studio\models"
```

## Command-line updater

Always inspect a plan first when scripting:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --dry-run
```

Update every safely downloadable pending model:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --yes
```

Update one or more exact model keys reported by the checker:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py `
  --model-key "publisher/model" `
  --model-key "publisher/other-model"
```

Useful updater flags:

- `--dry-run` validates and prints the complete plan without downloading.
- `--yes` is required for destructive execution without an interactive terminal.
- `--json` prints the plan or result as JSON.
- `--keep-backups` retains rollback files after successful validation.
- `--models-root`, `--state-file`, and `--timeout-seconds` override defaults.

The checker remains available independently:

```powershell
.\.venv\Scripts\python.exe .\lmstudio_weight_checker.py
.\.venv\Scripts\python.exe .\lmstudio_weight_checker.py --all
.\.venv\Scripts\python.exe .\lmstudio_weight_checker.py --json
```

## Safety, resume, and recovery

- Downloads are staged in `.weight-watcher-staging` beside the models root. Existing
  verified staged bytes are reused, while `huggingface_hub`/`hf_xet` resumes partial
  transfers in its local download metadata.
- Every staged file must match the remote byte count and SHA-256 before any installed
  file changes.
- Replacements use same-volume atomic moves. Original files receive a deterministic
  `.lmww-backup-<job>` name until the post-install checker confirms every selected
  model is up to date.
- Any install or validation failure restores all originals and restores the hash
  cache snapshot.
- On the next watcher or updater start, trusted job manifests are inspected. A
  missing destination with its exact rollback file is restored automatically. If
  both destination and rollback exist, no guess is made; the condition is reported
  for manual inspection.
- Cleanup removes only updater-owned files. Unknown files found in staging are
  preserved and reported.

Persistent watcher state defaults to
`%APPDATA%\LM Studio Weight Watcher\state.json`. Existing installs using the legacy
`LM Studio Weight Updater` state directory are recognized. Corrupt or older state is
sanitized and migrated without dropping valid acknowledge/snooze records.

## Start at login

Install the scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

Remove it:

```powershell
powershell -ExecutionPolicy Bypass -File .\remove_startup_task.ps1
```

The launcher checks `%LMSTUDIO_WATCHER_PYTHON%`, then local `.venv`/`venv` folders,
then `python`, then `py -3`. Hidden-launch failures go to `watcher-launch.log` and
Python errors to `watcher-error.log` beside the launcher.

## Tests

The suite uses fake downloaders and temporary model roots; it does not access real
Hugging Face repositories or mutate user model data.

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## License

This project is released under [The Unlicense](LICENSE).
