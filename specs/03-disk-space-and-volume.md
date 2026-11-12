# 3. Disk plan ignores backup copies and the staging volume

Priority: critical. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
sections 2 and 3.

## Observed

`build_update_plan` computes the space requirement as
`remaining_download_bytes + safety_reserve_bytes` and compares it to free space
on the staging volume (`lmstudio_weight_updater.py:457-464`) using
`shutil.disk_usage(_nearest_existing(staging_root))`.

Two problems.

**Backups are not counted.** During install, every artifact that already exists
on disk is renamed to `backup_path` and the staged copy moved into place. On the
same volume that is metadata-only for the moves, but the volume must hold both
copies at once until cleanup. A 20 GB quant with 21 GB free passes the check and
then fails partway through installation, after downloads are already spent. The
existing test `test_plan_rejects_invalid_oid_and_insufficient_space` only
injects a tiny `available_bytes`, so the new accounting is what the test has to
assert, not the rejection itself.

**The volume is assumed, never verified.** `staging_root_for` puts staging at
`models_root.parent / ".weight-watcher-staging"` (line 294-295), which for a
models root like `D:\models` is `D:\.weight-watcher-staging`. But `--models-root`
can point at a junction, a mounted folder, or a path whose parent is on another
volume, and then `os.replace` degrades from a rename into a cross-volume copy.
The atomic swap in `SPEC.md` section 5 silently becomes a non-atomic copy, and
the free-space check measures the wrong disk.

## Required

1. The space requirement MUST include the bytes of every artifact that already
   exists at its destination, because each becomes a backup copy during
   install. Formally: `remaining + existing_destination_bytes + reserve`.
2. The requirement MUST be computed from the plan's artifacts only. Unknown
   files in staging MUST NOT be counted (existing behavior, keep it).
3. `build_update_plan` MUST reject a plan whose staging root is on a different
   volume from `models_root`, with a `PlanError` that names both paths.
4. Volume identity MUST be determined by the platform's own notion of a volume:
   on Windows the drive of the resolved path, elsewhere the device id from
   `os.stat().st_dev`. `pathlib` drive comparison alone is not sufficient on
   POSIX.
5. Free space MUST be measured on the volume that will actually hold the
   backup copies, which is the destination volume. When staging and destination
   share a volume, the two measurements are the same. Surface both in the plan
   summary so the confirmation dialog can show the truth.
6. When space is insufficient, the error MUST state the required total, the
   available total, and how much of the requirement is backup space.
7. Existing comparison against `available_bytes` when the caller injects it
   MUST keep working: tests pass an explicit value and expect a rejection.
   Keep the parameter and its meaning, extended by requirement 1.
8. The safety reserve MUST remain configurable and MUST default to something
   proportional to reality. A flat 1 GiB reserve is not enough headroom for a
   100 GB artifact; make the effective reserve `max(safety_reserve_bytes,
   size of the largest planned artifact)` and document it.

## Out of scope

- Resumable-byte accounting. Existing behavior stands: a complete verified
  staged file reduces the requirement, an incomplete one does not.
- Cleaning up backups. That is spec 4.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_plan_requires_space_for_backup_copies` | one 100-byte download, 100-byte existing destination, configured reserve 0 (effective reserve 100) | `available_bytes=150` and `200` are insufficient; with `available_bytes=300` the plan builds |
| `test_plan_space_error_names_backup_component` | as above | message mentions the backup bytes distinctly from download bytes |
| `test_plan_rejects_cross_volume_staging` | patch the volume-identity helper so staging and root differ | `PlanError` naming both paths |
| `test_plan_accepts_same_volume_staging` | patch both to the same identity | plan builds |
| `test_plan_reserve_covers_largest_artifact` | reserve 1 byte, one 4096-byte artifact | effective requirement includes the 4096-byte floor |
| `test_plan_summary_reports_both_volumes` | normal plan | `summary_dict()` carries available bytes for the destination volume |

Cross-volume behavior needs a seam. Add a small helper that returns a volume
identity for a path and patch it in tests; do not create a second drive in a
test.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

`tests/test_lmstudio_alert_state.py` builds plans too. If its plans start
failing on volume identity, fix the test's setup rather than weakening
requirement 3, and say so in the commit message.
