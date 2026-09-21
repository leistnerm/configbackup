# Retention

ConfigBackup supports reusable named retention policies and fully local per-task retention.

## Policy resolution

A task can:

1. inherit a default named policy,
2. name a different policy with `retention_policy`,
3. override selected values with `retention`, or
4. omit `retention_policy` and define its entire policy locally.

The most specific task setting wins.

## Active and deleted lifecycle sections

A complete policy may contain separate `active` and `deleted` behavior:

```yaml
retention_policies:
  standard:
    active: {}
    deleted: {}
```

If a task defines retention without `active`/`deleted`, the shorthand is interpreted as the active policy.

## Safety floor: `min_versions`

`min_versions` is an absolute pruning safety floor for normal retention.

```yaml
min_versions: 3
```

The newest three versions of each source file are protected even if age, count, or size limits would otherwise remove them.

If a task's `max_size` cannot be reached without violating `min_versions`, ConfigBackup keeps the protected versions and logs a warning.

`min_versions` must be at least 1.

## Indefinite mode

```yaml
active:
  mode: indefinite
  min_versions: 3
```

Nothing is automatically pruned.

## Simple mode

```yaml
active:
  mode: simple
  min_versions: 3
  max_versions: 50
  max_age_days: 365
  max_size: 5GB
```

The limits are combined. A version may become eligible for pruning because of count, age, or the task-wide size ceiling, but `min_versions` remains protected.

`max_versions` is applied per logical source file.

`max_age_days` is applied to versions.

`max_size` applies to the task's retained version set, not individually to every file.

Supported size suffixes include `KB`, `MB`, `GB`, `TB` and binary `KiB`, `MiB`, `GiB`, `TiB`.

## Tiered mode

Tiered mode keeps dense recent history and thins older history.

```yaml
active:
  mode: tiered
  min_versions: 3
  tiers:
    - interval: all
      duration_days: 7
    - interval: weekly
      duration_days: 90
    - interval: monthly
      forever: true
```

`duration_days` is the span of that tier, not an absolute age boundary. The example means:

- age 0 through 6 days: keep all distinct versions,
- the next 90 days: keep one representative per ISO week,
- older: keep one representative per calendar month forever.

Supported intervals:

- `all`
- `daily`
- `weekly` (ISO week, Monday-Sunday)
- `monthly`
- `yearly`

For a daily/weekly/monthly/yearly bucket, the **newest** actual version in that bucket is retained.

A `forever: true` tier must be the last tier.

Tiered policies may also specify `max_versions`, `max_age_days`, or `max_size` as additional hard ceilings. `min_versions` still wins if a hard ceiling conflicts with the safety floor.

Example with more resolution:

```yaml
active:
  mode: tiered
  min_versions: 5
  tiers:
    - interval: all
      duration_days: 7
    - interval: daily
      duration_days: 30
    - interval: weekly
      duration_days: 365
    - interval: monthly
      duration_days: 1825
    - interval: yearly
      forever: true
```

## Named templates

```yaml
retention_policies:
  standard:
    active:
      mode: tiered
      min_versions: 3
      tiers:
        - interval: all
          duration_days: 7
        - interval: weekly
          duration_days: 90
        - interval: monthly
          forever: true
    deleted:
      mode: tiered
      min_versions: 3
      grace_days: 30
      tiers:
        - interval: all
          duration_days: 90
        - interval: monthly
          forever: true

defaults:
  retention_policy: standard
```

A task can then simply use the default, explicitly select a named policy, or override it:

```yaml
- name: sql-schema
  type: directory
  source: /var/tmp/sql-schema
  retention_policy: standard
  retention:
    active:
      min_versions: 10
      max_size: 50GB
    deleted:
      grace_days: 90
```

## Fully local policy

Templates are optional:

```yaml
- name: unusual-item
  type: file
  source: /opt/app/config.ini
  retention:
    active:
      mode: simple
      min_versions: 5
      max_versions: 20
    deleted:
      mode: indefinite
      min_versions: 5
      grace_days: 60
```

## Deleted objects

When deletion is confirmed, active version files move to:

```text
_deleted/YYYYMMDD/<task-name>/<logical-parent>/
```

and a `<filename>.deletion.<run-id>.json` sidecar records deletion metadata. The run ID prevents same-day delete/recreate/delete cycles from overwriting prior deletion metadata.

Old deleted generations remain separate if a source later reappears. A recreated source begins a new active history rather than merging the previous deleted generation back into the active tree.

## Deleted grace period

```yaml
deleted:
  grace_days: 30
```

During the grace period no retention thinning is performed on that deleted generation. This preserves detailed history immediately after deletion, when recovery/audit value is highest.

After the grace period, tiered thinning uses the versions' original timestamps.

For simple deleted `max_age_days`, age is measured from the deletion event so an old file is not immediately expired simply because its last content change happened years before deletion.

A deleted policy's `max_size` is enforced across eligible deleted generations for that task. Generations still inside `grace_days` are protected and are not forced under the size ceiling until their grace period expires.

## Optional complete purge of deleted generations

```yaml
deleted:
  purge_after_days: 2555
```

If configured, the entire deleted generation may be removed after that many days, including the `min_versions` floor. This is intentionally an explicit setting because it is the one retention feature allowed to remove every copy of a deleted object generation.

If `purge_after_days` is omitted, the normal minimum-version floor remains in effect indefinitely.

## Prune-only mode

Run retention without collectors/backups:

```bash
python3 configbackup.py -c configbackup.yaml --prune
```

Preview pruning:

```bash
python3 configbackup.py -c configbackup.yaml --prune --dry-run
```

## When automatic retention runs

During a normal backup invocation, automatic retention is deliberately conservative:

- if any required task fails or is skipped, automatic retention is skipped for the run;
- if optional tasks fail, successful tasks may still be retained/pruned normally, but failed/skipped tasks are not automatically pruned;
- `--prune` explicitly runs retention without a backup and is intended for deliberate maintenance/testing.

This prevents a failed collector or inaccessible source from being followed immediately by destructive retention activity.
