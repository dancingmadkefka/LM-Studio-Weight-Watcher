# Fix specs

One spec per defect found in the review of the update path. Each spec is
self-contained: observed behavior with line references, required behavior as
testable statements, out of scope, acceptance tests, verification commands.

These are not milestones. They are narrow fixes to existing code. Read
`SPEC.md` first for the invariants they serve, and `REVIEW.md` for how to review
one.

## Priority

Work top to bottom. The first four prevent a user from losing a model file or
from being told a model was deleted when it was not.

| # | Spec | Severity | Touches |
| --- | --- | --- | --- |
| 1 | [File-lock recovery can lose the original](01-file-lock-recovery.md) | Critical | `lmstudio_weight_updater.py` |
| 2 | [Rollback is best-effort and fails silently](02-rollback-integrity.md) | Critical | `lmstudio_weight_updater.py` |
| 3 | [Disk plan ignores backup copies and volume](03-disk-space-and-volume.md) | Critical | `lmstudio_weight_updater.py` |
| 4 | [Backups deleted before the swap is durable](04-backup-durability.md) | High | `lmstudio_weight_updater.py` |
| 5 | [Repos on a non-`main` branch look deleted](05-default-branch.md) | High | `lmstudio_weight_checker.py` |
| 6 | [Only a listing proves a file was removed](06-removal-proof.md) | High | `lmstudio_weight_checker.py` |
| 7 | [Recovery cannot finish an interrupted cleanup](07-recovery-completion.md) | High | `lmstudio_weight_updater.py` |
| 8 | [Tray recovery failures are invisible](08-watcher-recovery-surface.md) | High | `lmstudio_weight_watcher.py` |
| 9 | [The launcher runs a broken Python silently](09-launcher-preflight.md) | High | `run_watcher.bat`, `lmstudio_weight_watcher.py` |
| 10 | [Manifest temp file collides and leaks](10-manifest-atomic-write.md) | Medium | `lmstudio_weight_updater.py` |
| 11 | [Plan building re-hashes multi-GB staged files](11-staged-verification-marker.md) | Medium | `lmstudio_weight_updater.py` |
| 12 | [Downloader fills the shared HF cache](12-downloader-cache-dir.md) | Medium | `lmstudio_weight_updater.py` |
| 13 | [hf_xet absence is silent](13-xet-preflight.md) | Medium | `lmstudio_weight_updater.py` |
| 14 | [Hash cache cannot evict absolute-path keys](14-hash-cache-pruning.md) | Low | `lmstudio_weight_checker.py` |
| 15 | [Update orchestration is duplicated](15-shared-orchestration.md) | Medium | `lmstudio_weight_updater.py`, `lmstudio_weight_watcher.py` |
| 16 | [`execute` has outgrown one method](16-execute-decomposition.md) | Low | `lmstudio_weight_updater.py` |
| 17 | [Small hygiene items](17-small-hygiene.md) | Low | several |

## Suggested order

Bundle 1 (1, 2, 10) changes the install loop's failure handling in one place,
so do them together and review them once. Bundle 2 (3, 4) changes plan and
cleanup accounting. Bundle 3 (5, 6, 14) is the checker alone and can run in
parallel with the rest. Bundles 4 (7, 8, 15) and 5 (9, 17) are independent.

11, 12 and 13 touch the download and staging path. Do 12 before 11: once the
downloader writes its cache inside `download_dir`, the marker logic in 11 has to
decide what to do with those cache files.

## Rules for every spec

- No test may use the network. Fakes, fixtures, and injected failures only.
- No test may touch a real LM Studio models directory.
- Existing tests pass unchanged unless a spec explicitly says a test's
  expectation is wrong.
- A change that alters an on-disk format MUST keep reading the old format.
- Redaction applies to anything new that reaches a log, a manifest, or state.

## Facts several specs depend on

Verified against the current tree, so no spec has to re-derive them:

- `lmstudio_weight_updater.py` saves the hash cache at lines 1100 and 1127, so
  a CLI run saves it twice.
- The post-install success criterion is character-identical in two places:
  `lmstudio_weight_updater.py:1086` and `lmstudio_weight_watcher.py:702`.
- `execute` (lines 654-856) calls `write_manifest` at lines 658, 679, 708, 743,
  777, 790, 799, 820, 830, 842 and 849. The helper in spec 16 has to absorb all
  eleven.
- The manifest path is not currently read anywhere during a mutation, which is
  why spec 16's ordering assertion needs a new seam.

## Verification baseline

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Baseline before any of this work: 136 tests, all passing.
