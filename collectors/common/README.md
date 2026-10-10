# Snapshot comparison and representations

`compare_snapshots.py --before PATH --after PATH` distinguishes identical bytes, CSV row-order changes, narrow comparison-only changes, and remaining changes. It never rewrites source files.

`canonicalize.py` preserves native files unless explicitly asked to write a separate comparison output. Native PostgreSQL safety keys and arbitrary SQL order are preserved in the filesystem archive; Git comparison output may contain `<COMPARISON-ONLY>` and is not a restore script. Only known top-level psql restrict/unrestrict markers and narrow standalone extended-property batches are normalized. JSON object keys are sorted, arrays retain order; XML comparison uses canonical serialization without reordering elements.

`snapshot_manifest.py ROOT OUTPUT.json` records byte and comparison hashes; OUTPUT must be outside ROOT. Comparison hashes are not a general SQL semantic-equivalence proof.

See [release notes](../../docs/release-1.6.0.md) and [validation](../../VALIDATION.md).
