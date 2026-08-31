# Durable Memory Hygiene Operator Runbook

This command operates only on the active profile's built-in
`memories/MEMORY.md` and `memories/USER.md`. It does not inspect or mutate an
external memory provider. It is dry-run by default and does not alter a running
session's frozen memory snapshot; any applied result appears in newly started
sessions.

## Safety contract

- Target utilization must be between 1 and 74 percent; the default is 70.
- `USER.md` is always manual-review-only.
- Identity, user preference, security/approval, active runtime, and active
  operating-rule entries in `MEMORY.md` are never changed automatically.
- Safe automatic candidates are limited to normalized duplicates, entries with
  explicit superseded/obsolete markers, old entries with explicit
  temporary/completed markers, and lossless whitespace/boilerplate/repeated-
  sentence compaction.
- A dry run is required for operator review. `--apply` is the write boundary;
  without `--yes`, the operator must type the exact word `APPLY`.
- Apply locks both built-in stores, backs up both before the first live write,
  writes each file atomically, and restores both if any live write or audit
  receipt write fails.
- Every successful apply prints an audit receipt and an exact rollback command.
  Rollback refuses to overwrite memory changed after the apply receipt.

## 1. Dry-run and capture the complete report

```bash
hermes memory hygiene --target-percent 70 --stale-days 90 --json \
  > /tmp/hermes-memory-hygiene-report.json
```

Review these fields for both `memory` and `user`:

- `before_percent` and `projected_percent`
- `candidates`: everything identified, including candidates not needed to reach
  the target
- `protected`: entries excluded from automatic changes and the reason
- `plan`: the exact ordered changes that apply would make
- `target_met`: false means safe deterministic changes are insufficient; do not
  weaken the protections—manually curate the remaining entries instead

Running the same command without `--json` prints a concise terminal summary.

## 2. Apply the reviewed safe plan

Interactive boundary:

```bash
hermes memory hygiene --target-percent 70 --stale-days 90 --apply
```

Type `APPLY` exactly when prompted. For an already approved unattended run, the
explicit non-interactive boundary is:

```bash
hermes memory hygiene --target-percent 70 --stale-days 90 --apply --yes --json \
  > /tmp/hermes-memory-hygiene-apply.json
```

The command prints or returns:

- `backup_dir`: immutable pre-apply copies of `MEMORY.md` and `USER.md`
- `receipt_path`: JSON audit receipt with before/after SHA-256 values, utilization,
  protected-safe action list, and target status
- `rollback_command`: exact command bound to that receipt

Do not delete the backup directory while the receipt may need rollback.

## 3. Verify

```bash
hermes memory hygiene --target-percent 70 --stale-days 90 --json
```

Confirm the report has no unexpected plan and inspect the apply receipt:

```bash
python -m json.tool /absolute/path/from/receipt_path
```

Start a new Hermes session only after review if you need the compacted memory in
the frozen system-prompt snapshot. No gateway restart or deployment is required.

## 4. Roll back

Use the exact `rollback_command` from the apply receipt, for example:

```bash
hermes memory hygiene \
  --rollback /absolute/path/to/memories/audit/TRANSACTION.json \
  --yes
```

Without `--yes`, the command requires the exact word `ROLLBACK`. Before restore,
it verifies that each live file still matches the apply receipt's after-hash.
If either file has drifted, rollback fails closed and leaves both files
unchanged. A successful rollback creates a second audit receipt and preserves a
safety backup of the post-apply state.

## Recovery from a refused rollback

A drift refusal means another session or operator changed durable memory after
the hygiene apply. Do not force overwrite. Compare:

1. the current `MEMORY.md` / `USER.md`,
2. the apply receipt's `backup_dir`, and
3. the receipt's before/after hashes and actions.

Manually reconcile through the normal memory tool or a separately reviewed file
recovery. There is intentionally no force flag.
