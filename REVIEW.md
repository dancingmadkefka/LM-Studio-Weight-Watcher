# Updater Review Protocol

## 0. Review method

Review only the exact current milestone commit against `SPEC.md`. Inspect its parent
to establish scope. Run all listed automated checks and cite results. A MUST that is
not demonstrably satisfied is blocking. Reviewers MUST NOT use real model downloads
or modify user LM Studio data.

## M1 - Core updater and CLI

- MUST: updater is separate from the checker and plans per-artifact updates from
  checker output, including shards, nested paths, projectors, and deduplication.
- MUST: path containment, remote OID/size validation, disk-space checks, staging
  outside models root, and redaction-safe persisted manifests are tested.
- MUST: size/hash mismatch leaves installed files unchanged.
- MUST: multi-artifact replacement rolls back on injected failure and explains file
  locks without terminating LM Studio.
- MUST: CLI supports the options in SPEC section 6 and dry-run makes no downloads.
- MUST: cancellation is honored before commit and cannot interrupt commit/rollback.
- MUST: all M1 unit tests pass without network access.
- MUST NOT: add watcher UI behavior assigned to M2 beyond unavoidable interfaces.
- Automated checks: `python -m unittest tests.test_lmstudio_weight_updater -v` and
  `python -m unittest discover -s tests -v`.
- Human checks: none.

## M2 - Watcher UI and state integration

- MUST: state v1 migrates to v2 without losing alerts/snoozes and corrupt state is safe.
- MUST: selected/all update commands, confirmation, busy-state exclusion, progress,
  cancellation, post-update refresh, and shutdown-safe callbacks are covered.
- MUST: updater work never runs on the Tk event thread.
- MUST: one check/update mutation is active at a time.
- MUST: errors and locked-model guidance are visible and existing acknowledge/snooze
  behavior remains compatible.
- Automated checks: `python -m unittest tests.test_lmstudio_alert_state -v`,
  `python -m unittest tests.test_lmstudio_weight_watcher -v`, and full discovery.
- Human checks: none; mocked Tk lifecycle evidence is sufficient for this milestone.

## M3 - Documentation and end-to-end hardening

- MUST: README and requirements describe install/update/recovery behavior accurately.
- MUST: integration tests cover success, resume, hash/size failure, insufficient disk,
  locked destination, partial replacement rollback, recovery, and cleanup.
- MUST: existing checker/watcher CLI behavior remains compatible.
- MUST: complete test discovery passes without network access or user model mutation.
- SHOULD: static compilation succeeds for all Python modules.
- Automated checks: `python -m compileall -q .` and
  `python -m unittest discover -s tests -v`.
- Human checks: none.
