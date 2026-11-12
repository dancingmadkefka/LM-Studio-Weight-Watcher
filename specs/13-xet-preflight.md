# 13. hf_xet absence is silent

Priority: medium. Touches `lmstudio_weight_updater.py` and the README.

## Observed

`HuggingFaceDownloader.download` imports `huggingface_hub` and `tqdm`
(`lmstudio_weight_updater.py:568-575`) and mentions `hf_xet` only in the error
string:

```python
raise UpdateError(
    "Downloading requires huggingface_hub and hf_xet. "
    "Install the project's requirements and retry."
)
```

`hf_xet` is never imported and never checked. `requirements.txt` pins
`hf_xet>=1.1,<2`. When it is missing:

- `huggingface_hub` falls back to single-connection HTTP transfers, silently,
- the user gets none of the speedup the dependency exists for,
- the error string above tells them to install something that was never
  required to proceed.

`hf_xet` is a compiled wheel. A Python version or platform without a matching
wheel fails to install it, which is exactly the situation where a silent
fallback misleads.

## Required

1. Before the first download of a job, the updater MUST establish whether Xet
   acceleration is available.
2. When it is not, the updater MUST emit exactly one `ProgressEvent` whose
   message states that transfers will use the slower HTTP path and why. One
   event per job, not one per artifact.
3. The message MUST NOT be a failure and MUST NOT stop the update. A slow
   download is better than no download.
4. When Xet is available, no message is emitted. Do not announce normal
   operation.
5. The availability check MUST live behind a seam that tests can patch, and
   MUST NOT import `hf_xet` when `huggingface_hub` is absent.
6. The import error message MUST stop naming `hf_xet` as a requirement for
   downloading at all. Say that downloading requires `huggingface_hub`, and that
   `hf_xet` is optional for speed.
7. The README dependency note MUST match: `hf_xet` is recommended, not required,
   and the updater says so when it is missing.

## Out of scope

- Installing `hf_xet`. Never install packages on the user's behalf.
- Pinning behavior. `requirements.txt` already pins the version range.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_missing_xet_emits_one_warning` | patch the availability seam to false; two artifacts | exactly one progress event mentions the fallback |
| `test_present_xet_emits_no_warning` | seam true | no event mentions transfers |
| `test_missing_xet_still_downloads` | seam false, fake downloader | update succeeds |
| `test_availability_check_tolerates_missing_hub` | patch import machinery so `huggingface_hub` is unavailable | check returns false without raising |
| `test_import_error_message_no_longer_requires_xet` | trigger the `ImportError` path | message names `huggingface_hub` and calls `hf_xet` optional |

Capture events with a list-appending progress callback, as the other updater
tests do.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Then check the message text by eye in the CLI: run a dry-run, no; the warning
only appears during a download, so verify it from the test's captured event
rather than a real transfer.
