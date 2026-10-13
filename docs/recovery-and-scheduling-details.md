# Recovery and scheduling details

Use this complete distribution for the first deployment. Start with a new candidate configuration, inspect discovery suggestions, test connections under the intended scheduled account, then run and verify a small backup before enabling broader collection. Never mix files from different distributions. The existing collector, diagnostics, monitoring, secret-gate and system/service options remain described in README.md and their feature guides.

## Failure preservation and determinism

Failed, disabled and undiscovered sections remain protected from archive updates, deletion detection, Git writes and retention. A failed retention deletion rolls back its whole certified scope while healthy scopes proceed. This also applies to explicit prune-only runs. Disabled collector dependencies protect dependent archives from pruning. Collector cadence is bypassed when a dependent archive still needs a successful attempt; the collector produces a new manifest instead of reusing stale staged files.

The local undo journal records each immutable entry separately and syncs it before mutation. A failed before-image read cannot poison the following healthy file. Startup still stops if evidence is corrupt or Git HEAD changed. Interrupted uncommitted runs can restore the entire prior local transaction, then recollect healthy scopes. Filesystem durability depends on fsync/rename behavior; this is not a transaction across remote Git, network storage and collectors.

Native SQL bytes remain the recovery source. Comparison-only PostgreSQL normalization recognizes paired top-level restriction keys. It preserves carriage returns, line feeds and Unicode separators inside SQL strings. Recovery freshness includes the hashes of packaged application files, including same-version edits.

## Review menus and commands

The configuration editor's **Recovery and review workflows** menu includes retention/space planning, interrupted-run inspection, verified-copy retention and optional encrypted secondary copies. It also retains discovery choices, bounded include discovery, coverage, export refresh/freshness and repeated database collection. Save configuration changes before running saved-config workflows.

```sh
python retention_planning.py --config configbackup.yaml --output private/retention \
  --incoming-bytes 1073741824 --recovery-bytes 104857600
python journal_recovery.py --config configbackup.yaml --output private/journal
# After inspecting every proposed path, copy the exact plan_id from the report:
python journal_recovery.py --config configbackup.yaml --output private/journal-applied \
  --apply-plan PLAN_ID
```

The retention preview uses the engine's retention algorithm and current saved scope evidence. It lists keep/delete/protected files and reasons; no deletion is performed. Missing files are marked. The additional-space estimate budgets staging, a new archive and undo copies, plus recovery staging/copy. Specify realistic input sizes; zero means unknown. Git growth, filesystem overhead, compression, concurrent writers and unbounded external collectors are not modeled. Free space is reported separately for each path and must not be summed across the same volume.

The journal plan is tied to journal metadata and current target hashes and is rechecked under the run lock. Only unchanged-HEAD rollback or committed cleanup can be applied. Changed Git HEAD, corrupt evidence or orphan files produce a blocked report with preservation instructions. No automatic history rewrite is performed. Preserve archive, state, repository and journal together for manual reconciliation.

```sh
python discovery_review.py --config configbackup.yaml --output private/coverage \
  --state /archive/_configbackup/state.json --managed private/setup-exports \
  --drill-history private/drills/last-result.json
```

Coverage and recovery reports have searchable HTML tables and complete JSON evidence. File rows group by containing directory; task rows show shared connections, dependencies and last saved success/status. These are declared or observed relationships, not complete application dependency discovery. A recorded matching archive hash is not a fresh integrity verification. Freshness, secondary verification and supplied drill evidence remain distinct. Reports contain paths and credential-reference names; store them privately. They use no remote assets or telemetry.

## Managed encrypted secondary copies

```sh
python -m pip install -r requirements-encryption.txt
python recovery_maintenance.py refresh --config configbackup.yaml \
  --directory private/setup-exports --secondary /mounted/recovery-copy --encrypt-secondary
# Prompts without echo. For unattended use supply --password-env with a variable NAME.
python recovery_maintenance.py status --directory private/setup-exports \
  --password-env CONFIGBACKUP_RECOVERY_PASSPHRASE --output private/recovery-status
python recovery_maintenance.py retention --directory /mounted/recovery-copy \
  --keep 3 --password-env CONFIGBACKUP_RECOVERY_PASSPHRASE
# Inspect the preview, then repeat with --apply to reverify and prune.
```

Refresh verifies the exact encrypted artifact by authenticated decryption and bundle verification before publication. Repeated identical refresh reuses and verifies the existing encrypted copy. Local success survives secondary failure; exit 6 signals incomplete verification. Encryption uses the existing bounded AES-256-GCM/scrypt format; random salt/nonce are intentionally nondeterministic. Without a passphrase, status says current encrypted verification was not checked and returns 6. Plaintext local exports remain plaintext. Passphrase rotation and remote/cloud APIs are not automated.

Retention is opt-in, considers only recognized fingerprint names, protects at least one verified recovery copy and the local latest pointer, and rechecks a protected survivor before deletion. Corrupt, unrecognized, symlink and unverifiable encrypted copies are left for review. No verified recovery produces exit 6. This does not prove off-host placement or recovery-account key access. Run a real recovery drill under the intended account.

## Scheduling scenarios

The schedule analyzer writes `what-if.html` and `what-if.json` beside existing CSVs. Select a configured shift scenario to compare with baseline on a shared UTC timeline. Rows show source time zone, P95/override duration and load exclusion. The page reports conflicts, dependency conflicts, predicted deadline misses and unknown checks. It never edits scheduler definitions. `reports.what-if.exclude` applies to this view and its scenario calculations; disabling the report removes these outputs. Watchdogs/excluded loads remain visible unless explicitly report-excluded.

Use `analysis.max_visual_occurrences` (default 1000, range 1–10000) to bound each displayed timeline. CSV calculations retain their existing bounds. Unknown duration produces no bar; truncated output is labeled. Shifts can cross the original horizon, so prerequisite occurrences outside it remain unknown. These estimates are not overlap probabilities or a scheduling optimizer. See examples/schedule-report.yaml.

## Explicit destination changes

Retained task state records its configured destination. Filesystem-only moves preserve nested paths from that prefix and validate the proposed paths against the saved candidate. Legacy evidence requires `--from-destination`; the tool no longer guesses from common filenames. When moving a single file destination into a directory, explicitly supply the old parent prefix if the filename must be retained. Preview first. Git destination moves remain unsupported by the migration helper and require separately reviewed Git changes.

## Limits

Remote recovery transport, verified object-lock storage and automatic application restore are not provided.

## Missing PR worktrees and explicit journal reset

PR worktree cleanup happens after state and journal finalization. If a pending
run's worktree is missing, startup can restore the archive/state before-images
when the configured local PR branch still matches the journal's recorded commit.
The missing checkout is recreated by normal Git preparation. No branch is reset.
Changed/missing branch references, corrupt evidence and inaccessible existing
worktrees still require review. This avoids silently undoing already published work.

To explicitly abandon an interrupted run instead of undoing it:

```sh
python journal_recovery.py --config configbackup.yaml --output private/journal-reset --reset --acknowledge-incomplete-run
```

The command holds the run lock, copies current state files, and moves the journal
into a unique `transaction-abandoned-*` directory beside the active journal.
It does not delete recovery evidence, change Git, or repair partially updated
archive/state files. Rerun collection and check failures and publication status;
keep the evidence until the results are verified. The acknowledgement is required
because failed sections on the next run may retain incomplete data from the
abandoned run. Inspection without `--reset` remains read-only.
