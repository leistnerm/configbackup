#!/usr/bin/env python3
"""ConfigBackup PostgreSQL configuration/schema collector.

The collector uses PostgreSQL's native command-line clients (psql, pg_dump,
pg_dumpall) and writes a deterministic current-state tree. ConfigBackup is
responsible for history/versioning (filesystem, Git, or both).

Security principles:
- no password command-line/YAML option is provided;
- libpq authentication (.pgpass, service files, GSSAPI, certificates, etc.) is
  used normally;
- pg_dumpall is invoked with --no-role-passwords;
- known credential-bearing catalog/config values are redacted;
- raw server config files are only copied with an explicit opt-in because they
  may contain secrets.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

COLLECTOR_VERSION = "1.3.0"


class CollectorError(RuntimeError):
    pass


def info(message: str) -> None:
    print(f"[postgresql-collector] {message}")


def warn(message: str) -> None:
    print(f"[postgresql-collector] WARNING: {message}", file=sys.stderr)


def stable_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def stable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def normalize_scalar(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(str(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return value


def stable_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(str(k) for k in row.keys())
        fieldnames = sorted(keys)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        if fieldnames:
            writer.writeheader()
            for row in rows:
                writer.writerow({k: normalize_scalar(row.get(k)) for k in fieldnames})


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def safe_path_segment(value: str) -> str:
    """Return a Windows/Linux-safe readable path segment with collision suffix."""
    safe = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip().rstrip(" .")
    if not safe:
        safe = "_"
    reserved = re.match(r"^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", safe) is not None
    changed = safe != value or reserved
    if len(safe) > 100:
        safe = safe[:90]
        changed = True
    if changed:
        safe = f"{safe}__{short_hash(value)}"
    return safe


def split_values(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        result.extend(x.strip() for x in value.split(",") if x.strip())
    return result


def matches_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


_SECRET_NAME = re.compile(r"(?i)(password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|conninfo|connstr|connection[_-]?string)")
_SECRET_KV = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key)\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)"
)
_URI_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s/@:]+:)([^@\s/]+)(@)")


def redact_text(value: Any, *, name: str = "") -> Any:
    if value is None:
        return value
    text = str(value)
    if _SECRET_NAME.search(name):
        return "<REDACTED>"
    text = _SECRET_KV.sub(r"\1<REDACTED>", text)
    text = _URI_CREDENTIALS.sub(r"\1<REDACTED>\3", text)
    return text


def redact_mapping(row: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dict):
            clean[key] = redact_mapping(value)
        elif isinstance(value, list):
            clean[key] = [redact_mapping(v) if isinstance(v, dict) else redact_text(v, name=key) for v in value]
        else:
            clean[key] = redact_text(value, name=key) if value is not None else None
    return clean


def normalize_dump(text: str) -> str:
    """Remove only known volatile dump banner timestamps, preserving DDL."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if re.match(r"^-- (Started|Completed) on \d{4}-\d{2}-\d{2}", line):
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def quote_ident_pattern(identifier: str) -> str:
    """Quote one identifier for pg_dump's pattern parser."""
    return '"' + identifier.replace('"', '""') + '"'


class PgTools:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.bin_dir = Path(args.bin_dir).expanduser() if args.bin_dir else None
        self.env = os.environ.copy()
        if args.service:
            self.env["PGSERVICE"] = args.service
        if args.host:
            self.env["PGHOST"] = args.host
        if args.port:
            self.env["PGPORT"] = str(args.port)
        if args.user:
            self.env["PGUSER"] = args.user
        if args.sslmode:
            self.env["PGSSLMODE"] = args.sslmode
        # Prevent interactive password prompts in unattended runs.
        self.env.setdefault("PGCONNECT_TIMEOUT", str(args.connect_timeout))
        self._help_cache: dict[str, str] = {}

    def exe(self, name: str) -> str:
        candidate = str(self.bin_dir / (name + (".exe" if os.name == "nt" else ""))) if self.bin_dir else shutil.which(name)
        if not candidate or not Path(candidate).exists():
            raise CollectorError(f"Required PostgreSQL client tool not found: {name}")
        return candidate

    def run(
        self,
        name: str,
        arguments: list[str],
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.exe(name), *arguments]
        try:
            cp = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                timeout=timeout or self.args.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(f"{name} timed out after {timeout or self.args.command_timeout} seconds") from exc
        if check and cp.returncode != 0:
            detail = cp.stderr.strip() or cp.stdout.strip() or f"exit code {cp.returncode}"
            raise CollectorError(f"{name} failed: {detail}")
        return cp

    def help(self, name: str) -> str:
        if name not in self._help_cache:
            self._help_cache[name] = self.run(name, ["--help"], check=False, timeout=30).stdout
        return self._help_cache[name]

    def version(self, name: str) -> str:
        cp = self.run(name, ["--version"], check=True, timeout=30)
        return cp.stdout.strip()

    def psql_rows(self, database: str, query: str) -> list[dict[str, Any]]:
        query = query.strip().rstrip(";")
        wrapped = (
            "SELECT COALESCE(json_agg(row_to_json(_cbq)), '[]'::json)::text "
            f"FROM ({query}) AS _cbq;"
        )
        cp = self.run(
            "psql",
            ["-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1", "-d", database, "-c", wrapped],
        )
        text = cp.stdout.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"psql returned invalid JSON for database {database}: {exc}") from exc
        if not isinstance(value, list):
            raise CollectorError(f"Unexpected psql JSON shape for database {database}")
        return [dict(x) for x in value]

    def psql_scalar(self, database: str, expression: str) -> str:
        cp = self.run(
            "psql",
            ["-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1", "-d", database, "-c", f"SELECT {expression};"],
        )
        return cp.stdout.strip()

    def schema_dump(self, database: str) -> str:
        args = [
            "-w",
            "--schema-only",
            "--create",
            "--quote-all-identifiers",
            f"--lock-wait-timeout={self.args.lock_wait_timeout}",
        ]
        dump_help = self.help("pg_dump")
        if "--no-subscriptions" in dump_help:
            # Subscription connection strings can contain credentials. A redacted
            # subscription inventory is collected separately.
            args.append("--no-subscriptions")
        if "--restrict-key" in dump_help:
            # PostgreSQL documents this specifically for repeatable/comparable dumps.
            args.append("--restrict-key=ConfigBackup")
        args.extend(["--dbname", database])
        cp = self.run("pg_dump", args, timeout=self.args.dump_timeout)
        return normalize_dump(cp.stdout)

    def table_predata_dump(self, database: str, schema: str, table: str) -> str:
        pattern = f"{quote_ident_pattern(schema)}.{quote_ident_pattern(table)}"
        args = [
            "-w",
            "--schema-only",
            "--section=pre-data",
            "--quote-all-identifiers",
            "--no-owner",
            "--no-privileges",
            f"--lock-wait-timeout={self.args.lock_wait_timeout}",
            "--table",
            pattern,
        ]
        if "--restrict-key" in self.help("pg_dump"):
            args.append("--restrict-key=ConfigBackup")
        args.extend(["--dbname", database])
        cp = self.run("pg_dump", args, timeout=self.args.dump_timeout)
        return normalize_dump(cp.stdout)

    def globals_dump(self) -> str:
        help_text = self.help("pg_dumpall")
        if "--no-role-passwords" not in help_text:
            raise CollectorError(
                "pg_dumpall does not support --no-role-passwords; refusing to create globals.sql because role password hashes could be exposed"
            )
        args = ["-w", "--globals-only", "--no-role-passwords", "--quote-all-identifiers"]
        if "--restrict-key" in help_text:
            args.append("--restrict-key=ConfigBackup")
        args.extend(["--database", self.args.maintenance_db])
        cp = self.run("pg_dumpall", args, timeout=self.args.dump_timeout)
        return normalize_dump(cp.stdout)


def record_optional(
    failures: list[dict[str, str]],
    section: str,
    func,
    *,
    required: bool = False,
):
    try:
        return func()
    except Exception as exc:
        failures.append({"section": section, "error": str(exc), "required": str(required).lower()})
        warn(f"{section}: {exc}")
        if required:
            raise
        return None


def write_rows(root: Path, relative: str, rows: list[dict[str, Any]] | None, *, redact: bool = False) -> None:
    if rows is None:
        return
    if redact:
        rows = [redact_mapping(x) for x in rows]
    stable_csv(root / relative, rows)


def rows_to_object_files(
    root: Path,
    rows: list[dict[str, Any]],
    category: str,
    *,
    schema_key: str = "schema_name",
    name_key: str = "object_name",
    definition_key: str = "definition",
    identity_key: str | None = None,
) -> None:
    manifest: list[dict[str, Any]] = []
    for row in rows:
        schema = str(row.get(schema_key) or "_")
        name = str(row.get(name_key) or "_")
        identity = str(row.get(identity_key) or "") if identity_key else ""
        filename = safe_path_segment(name)
        if identity:
            filename += "__" + short_hash(identity)
        filename += ".sql"
        out = root / "objects" / category / safe_path_segment(schema) / filename
        stable_text(out, str(row.get(definition_key) or ""))
        manifest.append(
            {
                "schema": schema,
                "name": name,
                "identity": identity,
                "file": out.relative_to(root).as_posix(),
            }
        )
    stable_csv(root / "objects" / category / "_manifest.csv", manifest)


def sanitize_settings_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        name = str(row.get("name") or "")
        if "setting" in row:
            row["setting"] = redact_text(row["setting"], name=name)
        if "reset_val" in row:
            row["reset_val"] = redact_text(row["reset_val"], name=name)
        if "boot_val" in row:
            row["boot_val"] = redact_text(row["boot_val"], name=name)
        result.append(row)
    return result


def collect_cluster(tools: PgTools, root: Path, maintenance_db: str, failures: list[dict[str, str]], args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    server_rows = tools.psql_rows(
        maintenance_db,
        """
        SELECT current_database() AS maintenance_database,
               current_user AS connected_user,
               version() AS version,
               current_setting('server_version') AS server_version,
               current_setting('server_version_num') AS server_version_num,
               current_setting('data_directory') AS data_directory,
               current_setting('config_file') AS config_file,
               current_setting('hba_file') AS hba_file,
               current_setting('ident_file') AS ident_file
        """,
    )
    if not server_rows:
        raise CollectorError("PostgreSQL server did not return server metadata")
    server = server_rows[0]
    stable_json(root / "cluster" / "server.json", server)

    db_rows = tools.psql_rows(
        maintenance_db,
        """
        SELECT d.datname AS database_name,
               pg_get_userbyid(d.datdba) AS owner,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collate,
               d.datctype AS ctype,
               d.datistemplate AS is_template,
               d.datallowconn AS allow_connections,
               d.datconnlimit AS connection_limit,
               COALESCE(t.spcname, '') AS tablespace,
               d.datacl::text AS acl
        FROM pg_database d
        LEFT JOIN pg_tablespace t ON t.oid = d.dattablespace
        ORDER BY d.datname
        """,
    )
    stable_csv(root / "cluster" / "databases.csv", db_rows)

    roles = tools.psql_rows(
        maintenance_db,
        """
        SELECT rolname AS role_name,
               rolsuper AS superuser,
               rolinherit AS inherit,
               rolcreaterole AS create_role,
               rolcreatedb AS create_database,
               rolcanlogin AS can_login,
               rolreplication AS replication,
               rolconnlimit AS connection_limit,
               rolvaliduntil::text AS valid_until,
               rolconfig::text AS role_config
        FROM pg_roles
        ORDER BY rolname
        """,
    )
    for row in roles:
        row["role_config"] = redact_text(row.get("role_config"), name="role_config")
    stable_csv(root / "cluster" / "roles.csv", roles)

    memberships = tools.psql_rows(
        maintenance_db,
        """
        SELECT parent.rolname AS granted_role,
               member.rolname AS member_role,
               m.admin_option
        FROM pg_auth_members m
        JOIN pg_roles parent ON parent.oid = m.roleid
        JOIN pg_roles member ON member.oid = m.member
        ORDER BY parent.rolname, member.rolname
        """,
    )
    stable_csv(root / "cluster" / "role-memberships.csv", memberships)

    tablespaces = tools.psql_rows(
        maintenance_db,
        """
        SELECT t.spcname AS tablespace_name,
               pg_get_userbyid(t.spcowner) AS owner,
               pg_tablespace_location(t.oid) AS location,
               t.spcoptions::text AS options,
               t.spcacl::text AS acl
        FROM pg_tablespace t
        ORDER BY t.spcname
        """,
    )
    stable_csv(root / "cluster" / "tablespaces.csv", tablespaces)

    globals_sql = record_optional(failures, "cluster.globals", tools.globals_dump, required=True)
    if globals_sql is not None:
        stable_text(root / "cluster" / "globals.sql", globals_sql)

    settings = record_optional(
        failures,
        "config.pg_settings",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT name, setting, unit, category, context, vartype, source,
                   sourcefile, sourceline, pending_restart
            FROM pg_settings
            ORDER BY name
            """,
        ),
    )
    if settings is not None:
        stable_csv(root / "config" / "pg-settings.csv", sanitize_settings_rows(settings))

    file_settings = record_optional(
        failures,
        "config.pg_file_settings",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT sourcefile, sourceline, seqno, name, setting, applied, error
            FROM pg_file_settings
            ORDER BY sourcefile, sourceline, seqno
            """,
        ),
    )
    if file_settings is not None:
        stable_csv(root / "config" / "pg-file-settings.csv", sanitize_settings_rows(file_settings))

    hba = record_optional(
        failures,
        "config.pg_hba_file_rules",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT line_number, type, database::text, user_name::text, address,
                   netmask, auth_method, options::text, error
            FROM pg_hba_file_rules
            ORDER BY line_number
            """,
        ),
    )
    if hba is not None:
        stable_csv(root / "config" / "pg-hba-file-rules.csv", hba)

    ident = record_optional(
        failures,
        "config.pg_ident_file_mappings",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT line_number, map_name, sys_name, pg_username, error
            FROM pg_ident_file_mappings
            ORDER BY line_number
            """,
        ),
    )
    if ident is not None:
        stable_csv(root / "config" / "pg-ident-file-mappings.csv", ident)

    db_role_settings = record_optional(
        failures,
        "cluster.database_role_settings",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT COALESCE(d.datname, '*') AS database_name,
                   COALESCE(r.rolname, '*') AS role_name,
                   s.setconfig::text AS settings
            FROM pg_db_role_setting s
            LEFT JOIN pg_database d ON d.oid = s.setdatabase
            LEFT JOIN pg_roles r ON r.oid = s.setrole
            ORDER BY 1, 2
            """,
        ),
    )
    if db_role_settings is not None:
        for row in db_role_settings:
            row["settings"] = redact_text(row.get("settings"), name="role_settings")
        stable_csv(root / "cluster" / "database-role-settings.csv", db_role_settings)

    slots = record_optional(
        failures,
        "replication.slots",
        lambda: tools.psql_rows(
            maintenance_db,
            """
            SELECT slot_name, plugin, slot_type, database, temporary
            FROM pg_replication_slots
            ORDER BY slot_name
            """,
        ),
    )
    if slots is not None:
        stable_csv(root / "replication" / "slots.csv", slots)

    if args.include_sizes:
        sizes = record_optional(
            failures,
            "inventory.database_sizes",
            lambda: tools.psql_rows(
                maintenance_db,
                """
                SELECT datname AS database_name, pg_database_size(datname) AS size_bytes
                FROM pg_database
                WHERE datallowconn
                ORDER BY datname
                """,
            ),
        )
        if sizes is not None:
            stable_csv(root / "inventory" / "database-sizes.csv", sizes)

    if args.include_raw_config_files:
        copy_raw_config_files(root, server, file_settings or [], failures)

    return server, db_rows


def copy_raw_config_files(root: Path, server: dict[str, Any], file_settings: list[dict[str, Any]], failures: list[dict[str, str]]) -> None:
    paths: set[str] = set()
    for key in ("config_file", "hba_file", "ident_file"):
        value = server.get(key)
        if value:
            paths.add(str(value))
    for row in file_settings:
        if row.get("sourcefile"):
            paths.add(str(row["sourcefile"]))
    copied: list[dict[str, str]] = []
    for value in sorted(paths):
        source = Path(value)
        if not source.is_file():
            failures.append({"section": "config.raw", "error": f"Not locally readable: {value}", "required": "false"})
            continue
        out_name = safe_path_segment(source.name) + "__" + short_hash(str(source.resolve(strict=False)))
        destination = root / "config" / "raw" / out_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied.append({"server_path": value, "file": destination.relative_to(root).as_posix()})
    stable_csv(root / "config" / "raw" / "_path-map.csv", copied)


def select_databases(db_rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    includes = split_values(args.database)
    excludes = split_values(args.exclude_database)
    selected: list[dict[str, Any]] = []
    for row in db_rows:
        name = str(row["database_name"])
        if not bool(row.get("allow_connections")):
            continue
        if bool(row.get("is_template")) and not args.include_template_databases:
            continue
        if includes and not matches_any(name, includes):
            continue
        if excludes and matches_any(name, excludes):
            continue
        selected.append(row)
    return selected


def query_and_write(
    tools: PgTools,
    db: str,
    root: Path,
    relative: str,
    query: str,
    failures: list[dict[str, str]],
    section: str,
    *,
    redact: bool = False,
    required: bool = False,
) -> list[dict[str, Any]] | None:
    rows = record_optional(failures, section, lambda: tools.psql_rows(db, query), required=required)
    if rows is not None:
        write_rows(root, relative, rows, redact=redact)
    return rows


def collect_object_files(tools: PgTools, db: str, db_root: Path, server_version_num: int, failures: list[dict[str, str]], args: argparse.Namespace) -> None:
    if args.skip_object_files:
        return

    views = record_optional(
        failures,
        f"database.{db}.objects.views",
        lambda: tools.psql_rows(
            db,
            """
            SELECT n.nspname AS schema_name, c.relname AS object_name,
                   format('CREATE OR REPLACE VIEW %I.%I AS\n%s;', n.nspname, c.relname, pg_get_viewdef(c.oid, true)) AS definition
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'v'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname !~ '^pg_toast'
            ORDER BY n.nspname, c.relname
            """,
        ),
    )
    if views is not None:
        rows_to_object_files(db_root, views, "views")

    mviews = record_optional(
        failures,
        f"database.{db}.objects.materialized_views",
        lambda: tools.psql_rows(
            db,
            """
            SELECT n.nspname AS schema_name, c.relname AS object_name,
                   format('-- Normalized diff representation; schema.sql is authoritative.\nCREATE MATERIALIZED VIEW %I.%I AS\n%s\nWITH NO DATA;',
                          n.nspname, c.relname, pg_get_viewdef(c.oid, true)) AS definition
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'm'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname !~ '^pg_toast'
            ORDER BY n.nspname, c.relname
            """,
        ),
    )
    if mviews is not None:
        rows_to_object_files(db_root, mviews, "materialized-views")

    routines_query = """
        SELECT n.nspname AS schema_name,
               p.proname AS object_name,
               pg_get_function_identity_arguments(p.oid) AS identity,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_toast'
    """
    if server_version_num >= 110000:
        routines_query += " AND p.prokind IN ('f', 'p', 'w')"
    else:
        routines_query += " AND NOT p.proisagg"
    routines_query += " ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)"
    routines = record_optional(
        failures,
        f"database.{db}.objects.routines",
        lambda: tools.psql_rows(db, routines_query),
    )
    if routines is not None:
        rows_to_object_files(db_root, routines, "routines", identity_key="identity")

    indexes = record_optional(
        failures,
        f"database.{db}.objects.indexes",
        lambda: tools.psql_rows(
            db,
            """
            SELECT ns.nspname AS schema_name, idx.relname AS object_name,
                   pg_get_indexdef(i.indexrelid, 0, true) || ';' AS definition
            FROM pg_index i
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname !~ '^pg_toast'
            ORDER BY ns.nspname, idx.relname
            """,
        ),
    )
    if indexes is not None:
        rows_to_object_files(db_root, indexes, "indexes")

    constraints = record_optional(
        failures,
        f"database.{db}.objects.constraints",
        lambda: tools.psql_rows(
            db,
            """
            SELECT n.nspname AS schema_name,
                   c.conname AS object_name,
                   format('ALTER TABLE ONLY %I.%I ADD CONSTRAINT %I %s;',
                          n.nspname, r.relname, c.conname, pg_get_constraintdef(c.oid, true)) AS definition,
                   r.relname AS table_name
            FROM pg_constraint c
            JOIN pg_class r ON r.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            WHERE c.conrelid <> 0
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname !~ '^pg_toast'
            ORDER BY n.nspname, r.relname, c.conname
            """,
        ),
    )
    if constraints is not None:
        # Include table name in identity so same-named constraints don't collide.
        for row in constraints:
            row["identity"] = f"{row.get('table_name', '')}:{row.get('object_name', '')}"
        rows_to_object_files(db_root, constraints, "constraints", identity_key="identity")

    triggers = record_optional(
        failures,
        f"database.{db}.objects.triggers",
        lambda: tools.psql_rows(
            db,
            """
            SELECT n.nspname AS schema_name,
                   t.tgname AS object_name,
                   pg_get_triggerdef(t.oid, true) || ';' AS definition,
                   r.relname AS table_name
            FROM pg_trigger t
            JOIN pg_class r ON r.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname !~ '^pg_toast'
            ORDER BY n.nspname, r.relname, t.tgname
            """,
        ),
    )
    if triggers is not None:
        for row in triggers:
            row["identity"] = f"{row.get('table_name', '')}:{row.get('object_name', '')}"
        rows_to_object_files(db_root, triggers, "triggers", identity_key="identity")

    if args.split_table_ddl:
        tables = record_optional(
            failures,
            f"database.{db}.objects.table_list",
            lambda: tools.psql_rows(
                db,
                """
                SELECT n.nspname AS schema_name, c.relname AS table_name
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r','p','f')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname !~ '^pg_toast'
                ORDER BY n.nspname, c.relname
                """,
            ),
        )
        if tables is not None:
            manifest: list[dict[str, str]] = []
            for table_row in tables:
                schema = str(table_row["schema_name"])
                table = str(table_row["table_name"])
                try:
                    ddl = tools.table_predata_dump(db, schema, table)
                except Exception as exc:
                    failures.append({"section": f"database.{db}.table_ddl.{schema}.{table}", "error": str(exc), "required": "false"})
                    warn(f"database.{db}.table_ddl.{schema}.{table}: {exc}")
                    continue
                out = db_root / "objects" / "tables" / safe_path_segment(schema) / (safe_path_segment(table) + ".sql")
                stable_text(out, ddl)
                manifest.append({"schema": schema, "table": table, "file": out.relative_to(db_root).as_posix()})
            stable_csv(db_root / "objects" / "tables" / "_manifest.csv", manifest)


def collect_schedulers(tools: PgTools, db: str, db_root: Path, failures: list[dict[str, str]]) -> None:
    extensions = record_optional(
        failures,
        f"database.{db}.scheduler.extensions",
        lambda: tools.psql_rows(db, "SELECT extname FROM pg_extension WHERE extname IN ('pg_cron','pgagent') ORDER BY extname"),
    ) or []
    extnames = {str(r.get("extname")) for r in extensions}

    # pgAgent can be installed as a schema even when extension metadata differs.
    has_pgagent = record_optional(
        failures,
        f"database.{db}.scheduler.pgagent.detect",
        lambda: tools.psql_scalar(db, "CASE WHEN to_regnamespace('pgagent') IS NULL THEN 'false' ELSE 'true' END"),
    )
    if has_pgagent == "true":
        extnames.add("pgagent")

    if "pg_cron" in extnames:
        cron_rows = record_optional(
            failures,
            f"database.{db}.scheduler.pg_cron",
            lambda: tools.psql_rows(db, "SELECT * FROM cron.job ORDER BY jobid"),
        )
        if cron_rows is not None:
            # The command is intentionally retained; it is the scheduled configuration.
            # It may itself contain secrets, so SECURITY.md warns to protect the archive.
            stable_csv(db_root / "schedulers" / "pg-cron-jobs.csv", cron_rows)

    if "pgagent" in extnames:
        for table, out_name in [
            ("pga_job", "jobs.csv"),
            ("pga_jobstep", "job-steps.csv"),
            ("pga_schedule", "schedules.csv"),
            ("pga_jobclass", "job-classes.csv"),
        ]:
            rows = record_optional(
                failures,
                f"database.{db}.scheduler.pgagent.{table}",
                lambda table=table: tools.psql_rows(db, f"SELECT * FROM pgagent.{table} ORDER BY 1"),
            )
            if rows is None:
                continue
            sanitized: list[dict[str, Any]] = []
            for row in rows:
                clean = {}
                for key, value in row.items():
                    lower = key.lower()
                    if lower in {"joblastrun", "jobnextrun", "jobchanged", "jscnextrun"}:
                        continue
                    clean[key] = redact_text(value, name=key)
                sanitized.append(clean)
            stable_csv(db_root / "schedulers" / "pgagent" / out_name, sanitized)


def collect_database(tools: PgTools, row: dict[str, Any], root: Path, server_version_num: int, failures: list[dict[str, str]], args: argparse.Namespace) -> dict[str, str]:
    db = str(row["database_name"])
    safe_db = safe_path_segment(db)
    db_root = root / "databases" / safe_db
    db_root.mkdir(parents=True, exist_ok=True)
    stable_json(db_root / "database.json", row)

    if not args.skip_schema_dump:
        schema_sql = tools.schema_dump(db)  # required: fail collector if this fails
        stable_text(db_root / "schema.sql", schema_sql)

    query_and_write(
        tools,
        db,
        db_root,
        "inventory/extensions.csv",
        """
        SELECT e.extname AS extension_name, e.extversion AS version,
               n.nspname AS schema_name, e.extrelocatable AS relocatable
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        ORDER BY e.extname
        """,
        failures,
        f"database.{db}.extensions",
        required=True,
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/schemas.csv",
        """
        SELECT n.nspname AS schema_name, pg_get_userbyid(n.nspowner) AS owner, n.nspacl::text AS acl
        FROM pg_namespace n
        WHERE n.nspname !~ '^pg_toast'
        ORDER BY n.nspname
        """,
        failures,
        f"database.{db}.schemas",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/tables.csv",
        """
        SELECT n.nspname AS schema_name,
               c.relname AS object_name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
                    WHEN 'f' THEN 'foreign table' WHEN 'v' THEN 'view'
                    WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' ELSE c.relkind::text END AS object_type,
               pg_get_userbyid(c.relowner) AS owner,
               COALESCE(t.spcname, '') AS tablespace,
               c.relpersistence AS persistence,
               c.relrowsecurity AS row_security,
               c.relforcerowsecurity AS force_row_security
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
        WHERE c.relkind IN ('r','p','f','v','m','S')
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname !~ '^pg_toast'
        ORDER BY n.nspname, c.relname
        """,
        failures,
        f"database.{db}.tables",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/columns.csv",
        """
        SELECT table_schema, table_name, ordinal_position, column_name,
               data_type, udt_schema, udt_name, is_nullable, column_default,
               collation_name
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema, table_name, ordinal_position
        """,
        failures,
        f"database.{db}.columns",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/partitions.csv",
        """
        SELECT pn.nspname AS parent_schema, p.relname AS parent_table,
               cn.nspname AS child_schema, c.relname AS child_table,
               pg_get_expr(c.relpartbound, c.oid, true) AS partition_bound
        FROM pg_inherits i
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace pn ON pn.oid = p.relnamespace
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_namespace cn ON cn.oid = c.relnamespace
        WHERE pn.nspname NOT IN ('pg_catalog','information_schema')
        ORDER BY pn.nspname, p.relname, i.inhseqno, cn.nspname, c.relname
        """,
        failures,
        f"database.{db}.partitions",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/sequences.csv",
        """
        SELECT schemaname AS schema_name, sequencename AS sequence_name,
               sequenceowner AS owner, data_type, start_value, min_value,
               max_value, increment_by, cycle, cache_size
        FROM pg_sequences
        WHERE schemaname NOT IN ('pg_catalog','information_schema')
        ORDER BY schemaname, sequencename
        """,
        failures,
        f"database.{db}.sequences",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/policies.csv",
        """
        SELECT schemaname AS schema_name, tablename AS table_name, policyname AS policy_name,
               permissive, roles::text, cmd, qual, with_check
        FROM pg_policies
        ORDER BY schemaname, tablename, policyname
        """,
        failures,
        f"database.{db}.policies",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/event-triggers.csv",
        """
        SELECT e.evtname AS event_trigger_name, e.evtevent AS event,
               e.evtenabled AS enabled_mode, e.evttags::text AS tags,
               n.nspname AS function_schema, p.proname AS function_name
        FROM pg_event_trigger e
        JOIN pg_proc p ON p.oid = e.evtfoid
        JOIN pg_namespace n ON n.oid = p.pronamespace
        ORDER BY e.evtname
        """,
        failures,
        f"database.{db}.event_triggers",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/languages.csv",
        """
        SELECT lanname AS language_name, lanpltrusted AS trusted,
               pg_get_userbyid(lanowner) AS owner
        FROM pg_language
        ORDER BY lanname
        """,
        failures,
        f"database.{db}.languages",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/foreign-data-wrappers.csv",
        """
        SELECT foreign_data_wrapper_name, authorization_identifier, library_name, foreign_data_wrapper_language
        FROM information_schema.foreign_data_wrappers
        ORDER BY foreign_data_wrapper_name
        """,
        failures,
        f"database.{db}.fdw",
    )
    query_and_write(
        tools,
        db,
        db_root,
        "inventory/foreign-servers.csv",
        """
        SELECT foreign_server_name, authorization_identifier, foreign_data_wrapper_name,
               foreign_server_type, foreign_server_version
        FROM information_schema.foreign_servers
        ORDER BY foreign_server_name
        """,
        failures,
        f"database.{db}.foreign_servers",
    )
    user_mapping_options = record_optional(
        failures,
        f"database.{db}.user_mapping_options",
        lambda: tools.psql_rows(
            db,
            """
            SELECT authorization_identifier, foreign_server_name, option_name, option_value
            FROM information_schema.user_mapping_options
            ORDER BY authorization_identifier, foreign_server_name, option_name
            """,
        ),
    )
    if user_mapping_options is not None:
        for option in user_mapping_options:
            option_name = str(option.get("option_name") or "")
            option["option_value"] = redact_text(option.get("option_value"), name=option_name)
        stable_csv(db_root / "inventory" / "user-mapping-options.csv", user_mapping_options)

    if server_version_num >= 100000:
        publications = record_optional(
            failures,
            f"database.{db}.publications",
            lambda: tools.psql_rows(
                db,
                """
                SELECT pubname AS publication_name, pg_get_userbyid(pubowner) AS owner,
                       puballtables AS all_tables, pubinsert AS publish_insert,
                       pubupdate AS publish_update, pubdelete AS publish_delete
                FROM pg_publication
                ORDER BY pubname
                """,
            ),
        )
        if publications is not None:
            stable_csv(db_root / "replication" / "publications.csv", publications)

        subscriptions = record_optional(
            failures,
            f"database.{db}.subscriptions",
            lambda: tools.psql_rows(
                db,
                """
                SELECT subname AS subscription_name, pg_get_userbyid(subowner) AS owner,
                       subenabled AS enabled, subslotname AS slot_name,
                       subsynccommit AS synchronous_commit, subpublications::text AS publications,
                       '<REDACTED>'::text AS connection_info
                FROM pg_subscription
                ORDER BY subname
                """,
            ),
        )
        if subscriptions is not None:
            stable_csv(db_root / "replication" / "subscriptions.csv", subscriptions)

    if args.include_sizes:
        relation_sizes = record_optional(
            failures,
            f"database.{db}.relation_sizes",
            lambda: tools.psql_rows(
                db,
                """
                SELECT n.nspname AS schema_name, c.relname AS relation_name,
                       c.relkind, pg_total_relation_size(c.oid) AS total_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r','p','m','i')
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname !~ '^pg_toast'
                ORDER BY n.nspname, c.relname
                """,
            ),
        )
        if relation_sizes is not None:
            stable_csv(db_root / "inventory" / "relation-sizes.csv", relation_sizes)

    collect_object_files(tools, db, db_root, server_version_num, failures, args)
    if not args.skip_schedulers:
        collect_schedulers(tools, db, db_root, failures)

    return {"database": db, "directory": safe_db}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect deterministic PostgreSQL configuration/schema state for ConfigBackup."
    )
    parser.add_argument("--output", help="Output directory; defaults to CONFIGBACKUP_OUTPUT")
    parser.add_argument("--host", help="PostgreSQL host (or use libpq environment/service configuration)")
    parser.add_argument("--port", type=int, help="PostgreSQL port")
    parser.add_argument("--user", help="PostgreSQL user")
    parser.add_argument("--service", help="libpq service name")
    parser.add_argument("--sslmode", help="libpq sslmode")
    parser.add_argument("--maintenance-db", default="postgres", help="Database used for cluster-level queries (default: postgres)")
    parser.add_argument("--database", action="append", help="Database name/pattern; repeat or comma-separate. Default: all connectable non-template DBs")
    parser.add_argument("--exclude-database", action="append", help="Database name/pattern to exclude; repeat or comma-separate")
    parser.add_argument("--include-template-databases", action="store_true")
    parser.add_argument("--bin-dir", help="Directory containing psql/pg_dump/pg_dumpall")
    parser.add_argument("--skip-schema-dump", action="store_true", help="Skip authoritative pg_dump --schema-only files")
    parser.add_argument("--skip-object-files", action="store_true", help="Skip per-object diff-oriented SQL files")
    parser.add_argument("--split-table-ddl", action="store_true", help="Also run pg_dump pre-data per table (more expensive)")
    parser.add_argument("--skip-schedulers", action="store_true", help="Skip pg_cron/pgAgent discovery")
    parser.add_argument("--include-sizes", action="store_true", help="Include volatile database/relation size inventories")
    parser.add_argument(
        "--include-raw-config-files",
        action="store_true",
        help="Copy locally readable raw PostgreSQL config files. WARNING: raw config may contain secrets.",
    )
    parser.add_argument("--connect-timeout", type=int, default=15)
    parser.add_argument("--command-timeout", type=int, default=120)
    parser.add_argument("--dump-timeout", type=int, default=3600)
    parser.add_argument("--lock-wait-timeout", default="30s", help="pg_dump lock wait timeout (default: 30s)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {COLLECTOR_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_value = args.output or os.environ.get("CONFIGBACKUP_OUTPUT")
    if not output_value:
        print("ERROR: --output or CONFIGBACKUP_OUTPUT is required", file=sys.stderr)
        return 2
    root = Path(output_value).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True)

    failures: list[dict[str, str]] = []
    tools = PgTools(args)
    try:
        tool_versions = {name: tools.version(name) for name in ("psql", "pg_dump", "pg_dumpall")}
        server, db_rows = collect_cluster(tools, root, args.maintenance_db, failures, args)
        server_version_num = int(server.get("server_version_num") or 0)
        selected = select_databases(db_rows, args)
        if not selected:
            raise CollectorError("No connectable databases matched the configured selection")

        database_map: list[dict[str, str]] = []
        for row in selected:
            db = str(row["database_name"])
            info(f"Collecting database {db}")
            # The schema dump and core extension inventory are required. If either
            # fails, abort the collector so ConfigBackup never archives a partial
            # snapshot as if objects were deleted.
            database_map.append(collect_database(tools, row, root, server_version_num, failures, args))
        stable_csv(root / "database-map.csv", database_map)

        collector_meta = {
            "collector": "ConfigBackup PostgreSQL collector",
            "collector_version": COLLECTOR_VERSION,
            "server_version": server.get("server_version"),
            "server_version_num": server.get("server_version_num"),
            "maintenance_database": args.maintenance_db,
            "selected_databases": [x["database"] for x in database_map],
            "tool_versions": tool_versions,
            "options": {
                "skip_schema_dump": args.skip_schema_dump,
                "skip_object_files": args.skip_object_files,
                "split_table_ddl": args.split_table_ddl,
                "skip_schedulers": args.skip_schedulers,
                "include_sizes": args.include_sizes,
                "include_raw_config_files": args.include_raw_config_files,
            },
            "warnings": failures,
        }
        stable_json(root / "collector.json", collector_meta)
        info(f"Collected {len(database_map)} database(s) into {root}")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Write deterministic failure details if possible, then fail hard so the
        # dependent ConfigBackup task is skipped and deletion state cannot advance.
        try:
            failures.append({"section": "fatal", "error": str(exc), "required": "true"})
            stable_json(root / "collector-error.json", {"collector_version": COLLECTOR_VERSION, "failures": failures})
        except Exception:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
