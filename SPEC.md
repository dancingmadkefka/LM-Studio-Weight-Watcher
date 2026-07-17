# LM Studio Weight Watcher Updater Specification

## 1. Purpose

Extend the existing read-only LM Studio Weight Watcher with safe, user-triggered
downloads and installation of changed Hugging Face artifacts. The checker remains
the authority for artifact discovery and remote identities; update execution lives
in a separate service usable from both the tray application and a headless CLI.

## 2. Safety invariants

- Installed files MUST NOT be modified until every artifact in the selected model
  transaction has the expected byte size and SHA-256.
- Download staging MUST be outside the LM Studio models directory and on the same
  volume as the destination so final moves do not become cross-volume copies.
- Every destination MUST resolve beneath the configured models root.
- A failed multi-artifact installation MUST restore every artifact already changed.
- Rollback copies MUST remain until a post-install checker pass confirms the model
  is up to date.
- Unknown files MUST NOT be deleted automatically.
- The application MUST NOT terminate LM Studio or forcibly unload a model.
- Authentication tokens and signed download URLs MUST NOT be written to logs/state.

## 3. Update planning

An update plan is built from `CheckResult.artifacts` for explicitly selected model
keys. It includes artifacts whose status is `update-available` or `missing-local`
only when the remote file, expected size, and a 64-character SHA-256 LFS OID are
available. Unresolved and removed-remote artifacts are rejected with an actionable
message.

Plans MUST preserve nested remote paths, include every changed shard/projector, and
deduplicate shared artifacts by normalized destination plus remote OID. Conflicting
remote identities for one destination MUST reject the plan. Disk-space calculation
MUST account for resumable staged bytes and retain a safety reserve.

## 4. Download, verification, and recovery

Use `huggingface_hub` and `hf_xet` for accelerated, resumable downloads. Staging is
under `<models-root-parent>/.weight-watcher-staging`, with deterministic job and
artifact manifests. Downloads expose progress and accept cancellation; cancellation
is permitted before installation starts.

Every completed download is independently checked for exact size and SHA-256. Job
manifests record only recovery-relevant paths, OIDs, sizes, and phases. At startup,
safe incomplete downloads can resume, interrupted installations are inspected, and
missing destinations with a known rollback copy are restored. Abandoned data is
reported or explicitly cleaned; it is never guessed at by filename glob alone.

## 5. Transactional installation

All artifacts for one selected model are verified before installation. Existing
destinations move to unique rollback paths, then staged artifacts move into place.
If any operation fails, prior replacements are rolled back in reverse order. Windows
file-lock errors surface as "Unload the model in LM Studio and retry." After a
successful post-install checker pass, rollback files and manifests are removed and
the verified OIDs seed the local hash cache.

Bulk selection may contain multiple model transactions. Shared files install once.
A failure in one model MUST be reported without corrupting already completed models.

## 6. CLI

`lmstudio_weight_updater.py` provides `--all`, repeatable `--model-key`, `--dry-run`,
`--yes`, `--keep-backups`, `--models-root`, `--state-file`, and `--timeout-seconds`.
Destructive execution requires `--yes` in non-interactive contexts. Dry-run prints a
machine-readable/update-readable plan without downloading.

## 7. Watcher UI

The alert window adds `Update Selected` and `Update All Pending`. A confirmation view
shows models, changed files, total bytes, available space, and upstream commit data.
Updates run off the Tk thread and publish progress through `root.after`. Only one
update/check mutation runs at a time. Buttons reflect busy state; cancellation is
available while downloading/verifying but disabled during installation/rollback.

Rows/status expose queued, downloading, verifying, installing, waiting-for-unload,
updated, cancelled, and failed states. Completion immediately runs a fresh check,
updates persistent alerts, and refreshes the tray/window.

## 8. Persistent state

Alert state migrates compatibly from version 1 to version 2. It stores a bounded
`last_update` summary and active recovery metadata without discarding existing alert
acknowledgment/snooze information. Corrupt or legacy state continues to load safely.

## 9. Documentation and compatibility

README usage and safety behavior are updated. Existing checker and watcher commands
remain compatible. The normal test suite MUST run without real Hugging Face network
access; downloader and failure cases use fakes/fixtures.

## 10. Milestones

### M1 - Core updater and CLI

Implement planning, disk validation, staging manifests, download abstraction,
verification, cancellation, transactional replacement/rollback, recovery inspection,
hash-cache seeding, and the CLI. Add focused core tests.

### M2 - Watcher UI and state integration

Migrate state v1 to v2, integrate update controls and worker lifecycle, confirmation,
progress/cancellation, post-update refresh, and shutdown handling. Add UI/state tests.

### M3 - Documentation and end-to-end hardening

Complete dependency/launcher documentation, fault-injection and integration tests,
cleanup/recovery coverage, full-suite verification, and final compatibility review.
