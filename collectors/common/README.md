# Snapshot comparison

`compare_snapshots.py --before PATH --after PATH` compares two separately collected source trees offline and detects exact unchanged files and CSV row-order-only differences. It does not rewrite T-SQL or DDL; source code and SQL statement reordering may have semantic effects.
