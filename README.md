# LM Studio Weight Watcher

LM Studio Weight Watcher tells you when a model you already have in LM Studio has newer weights on Hugging Face.

It does not download or replace anything. It only checks and alerts.

## Why It Exists

LM Studio downloads are snapshots. If the upstream model changes later, there is no built-in persistent update alert. This fills that gap.

## Usage

Run once:

```powershell
python .\lmstudio_weight_checker.py
```

Show all checked models:

```powershell
python .\lmstudio_weight_checker.py --all
```

Output JSON:

```powershell
python .\lmstudio_weight_checker.py --json
```

Use a custom models folder:

```powershell
python .\lmstudio_weight_checker.py --models-root "D:\LM Studio models\.cache\lm-studio\models"
```

Run the watcher once and update alert state:

```powershell
python .\lmstudio_weight_watcher.py --once
```

Start the tray app:

```powershell
python .\lmstudio_weight_watcher.py
```

Use the launcher without activating Conda:

```powershell
.\run_watcher.bat
```

Start at login:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

Remove the startup task:

```powershell
powershell -ExecutionPolicy Bypass -File .\remove_startup_task.ps1
```

Change the check interval or reminder timing:

```powershell
python .\lmstudio_weight_watcher.py --check-interval-hours 4 --reminder-interval-minutes 30
```

Change the watcher timeout or update tolerance:

```powershell
python .\lmstudio_weight_watcher.py --timeout-seconds 45 --tolerance-seconds 90
```

## Notes

- By default, the script discovers the models folder from `%APPDATA%\LM Studio\settings.json`.
- It uses a 60 second tolerance window to avoid false positives from timestamp rounding or metadata lag.
- Embedding models are skipped by default because LM Studio reports them differently. Regular chat and multimodal models still work.
- Persistent watcher state is stored by default in `%APPDATA%\LM Studio Weight Watcher\state.json`.
- Existing installs using the old `LM Studio Weight Updater` state path are still recognized automatically.
- The launcher prefers `%LMSTUDIO_WATCHER_PYTHON%`, then `%USERPROFILE%\miniforge3\envs\weightupdater`, then `%USERPROFILE%\miniforge3`.
- If the hidden launcher has to fall back to `pythonw` from `PATH`, it writes a note to `watcher-launch.log`.

## License

This project is released under [The Unlicense](LICENSE).
