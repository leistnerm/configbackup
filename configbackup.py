#!/usr/bin/env python3
"""ConfigBackup - cross-platform versioned configuration backup utility.

Backs up files/directories/globs and can run collector scripts/commands before
or during backup processing. Content-addressed change detection avoids storing
identical versions. Retention can be indefinite, simple, or tiered.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fnmatch
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

APP_NAME = "ConfigBackup"
APP_VERSION = "1.4.0"
STATE_VERSION = 1
PHASES = ["pre_run", "pre_backup", "backup", "post_backup", "post_run"]
TASK_TYPES = {"execute", "command", "file", "directory", "glob"}
INTERVALS = {"all", "daily", "weekly", "monthly", "yearly"}
VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?I?B)?\s*$", re.I)
GLOB_META_RE = re.compile(r"[*?[]")


class ConfigError(Exception):
    pass


class LockError(Exception):
    pass


@dataclass
class TaskResult:
    name: str
    status: str  # success, failed, skipped, dry-run
    message: str = ""
    required: bool = True
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    started: str | None = None
    ended: str | None = None
    duration_seconds: float = 0.0
    return_code: int | None = None


@dataclass
class RunStats:
    files_scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    deleted: int = 0
    stored: int = 0
    pruned: int = 0
    bytes_written: int = 0
    bytes_pruned: int = 0


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def parse_size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            raise ConfigError("Size values cannot be negative")
        return value
    if isinstance(value, float):
        if value < 0:
            raise ConfigError("Size values cannot be negative")
        return int(value)
    match = SIZE_RE.match(str(value))
    if not match:
        raise ConfigError(f"Invalid size value: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    multipliers = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "PB": 1000**5,
        "EB": 1000**6,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
        "PIB": 1024**5,
        "EIB": 1024**6,
    }
    return int(number * multipliers[unit])


def human_size(value: int) -> str:
    n = float(value)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(n) < 1000.0 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{value} B"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def sanitize_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "task"


def has_glob_magic(value: str) -> bool:
    return bool(GLOB_META_RE.search(value))


def glob_static_base(pattern: str) -> Path:
    """Return the non-glob prefix directory used to anchor a recursive glob."""
    # Preserve Windows drive prefix when present.
    win = PureWindowsPath(pattern) if re.match(r"^[A-Za-z]:[\\/]", pattern) else None
    if win is not None:
        parts = list(win.parts)
        kept = []
        for part in parts:
            if has_glob_magic(part):
                break
            kept.append(part)
        if not kept:
            return Path(win.anchor or ".")
        candidate = PureWindowsPath(*kept)
        # If the last static component looks like a filename only because the
        # glob starts in a later component, it is still a directory anchor.
        return Path(str(candidate))
    p = Path(pattern)
    kept: list[str] = []
    anchor = p.anchor
    for part in p.parts:
        if part == anchor:
            continue
        if has_glob_magic(part):
            break
        kept.append(part)
    if anchor:
        return Path(anchor, *kept) if kept else Path(anchor)
    return Path(*kept) if kept else Path(".")


def expand_string(value: str, variables: dict[str, str], strict: bool = True) -> str:
    expanded = value
    for _ in range(10):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in variables:
                return str(variables[key])
            if key in os.environ:
                return os.environ[key]
            if strict:
                raise ConfigError(f"Unknown variable ${{{key}}}")
            return match.group(0)

        updated = VARIABLE_RE.sub(replace, expanded)
        if updated == expanded:
            break
        expanded = updated
    if strict and VARIABLE_RE.search(expanded):
        unresolved = ", ".join(sorted(set(VARIABLE_RE.findall(expanded))))
        raise ConfigError(f"Unresolved or recursive variable(s): {unresolved}")
    return os.path.expanduser(os.path.expandvars(expanded))


def expand_value(value: Any, variables: dict[str, str], strict: bool = True) -> Any:
    if isinstance(value, str):
        return expand_string(value, variables, strict)
    if isinstance(value, list):
        return [expand_value(v, variables, strict) for v in value]
    if isinstance(value, dict):
        return {k: expand_value(v, variables, strict) for k, v in value.items()}
    return value


def hash_file(path: Path, algorithm: str = "sha256", chunk_size: int = 1024 * 1024) -> str:
    try:
        h = hashlib.new(algorithm)
    except ValueError as exc:
        raise ConfigError(f"Unsupported hash algorithm: {algorithm}") from exc
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def split_for_version(filename: str) -> tuple[str, str]:
    # Timestamp is inserted before the final suffix only.
    if filename in {".", ".."}:
        return filename, ""
    p = Path(filename)
    suffix = p.suffix
    if suffix and suffix != filename:
        return filename[: -len(suffix)], suffix
    return filename, ""


def make_versioned_filename(filename: str, timestamp: dt.datetime, existing_names: set[str]) -> str:
    stem, suffix = split_for_version(filename)
    date_part = timestamp.strftime("%Y%m%d")
    candidate = f"{stem}.{date_part}{suffix}"
    if candidate not in existing_names:
        return candidate
    time_part = timestamp.strftime("%H%M%S")
    candidate = f"{stem}.{date_part}-{time_part}{suffix}"
    if candidate not in existing_names:
        return candidate
    i = 1
    while True:
        candidate = f"{stem}.{date_part}-{time_part}-{i:02d}{suffix}"
        if candidate not in existing_names:
            return candidate
        i += 1


def source_to_logical(path: Path) -> Path:
    """Map an absolute platform path to a relative archive path."""
    raw = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        wp = PureWindowsPath(raw)
        drive = wp.drive.rstrip(":\\/")
        parts = [drive] + [p for p in wp.parts[1:] if p not in {"\\", "/"}]
        return Path(*parts)
    if raw.startswith("\\\\"):
        wp = PureWindowsPath(raw)
        anchor = wp.anchor.strip("\\").replace("\\", "_")
        rest = [p for p in wp.parts if p not in {wp.anchor, "\\", "/"}]
        return Path("UNC", sanitize_component(anchor), *rest)
    resolved = path.absolute()
    parts = [p for p in resolved.parts if p not in {resolved.anchor, "/", "\\"}]
    return Path(*parts)


def posix_rel(path: Path) -> str:
    return path.as_posix().lstrip("/")


def path_matches(rel: str, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    pp = PurePosixPath(rel)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if fnmatch.fnmatch(rel, normalized) or pp.match(normalized):
            return True
        if normalized.startswith("**/"):
            short = normalized[3:]
            if fnmatch.fnmatch(rel, short) or pp.match(short):
                return True
    return False


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_json_write(path: Path, payload: Any) -> None:
    ensure_parent(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_copy(src: Path, dst: Path) -> None:
    ensure_parent(dst)
    fd, tmp_name = tempfile.mkstemp(prefix=".configbackup-", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp, follow_symlinks=True)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


@contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    ensure_parent(path)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockError("Another ConfigBackup instance is already running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockError("Another ConfigBackup instance is already running") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


class ConfigLoader:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if yaml is None:
            raise ConfigError("PyYAML is required. Install with: pip install PyYAML")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        except FileNotFoundError as exc:
            raise ConfigError(f"Configuration file not found: {self.path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Top-level YAML value must be a mapping")
        return self._resolve(raw)

    def _resolve(self, cfg: dict[str, Any]) -> dict[str, Any]:
        cfg = copy.deepcopy(cfg)

        for section in ["backup", "internal", "options", "logging", "deletion", "variables", "retention_policies", "defaults", "git"]:
            if section in cfg and cfg[section] is not None and not isinstance(cfg[section], dict):
                raise ConfigError(f"{section} must be a mapping")

        variables_raw = cfg.get("variables") or {}
        variables = {str(k): str(v) for k, v in variables_raw.items()}
        # Resolve config variables recursively a few times so variables may reference variables.
        for _ in range(10):
            changed = False
            for k, v in list(variables.items()):
                nv = expand_string(v, variables, strict=False)
                if nv != v:
                    variables[k] = nv
                    changed = True
            if not changed:
                break
        cfg["variables"] = variables

        backup = cfg.setdefault("backup", {})
        if "root" not in backup:
            raise ConfigError("backup.root is required")
        backup["root"] = expand_string(str(backup["root"]), variables)
        backup.setdefault("include_hostname", False)
        backup.setdefault("hostname", socket.gethostname())
        backup.setdefault("deleted_directory", "_deleted")

        internal = cfg.setdefault("internal", {})
        internal.setdefault("directory", "_configbackup")
        internal.setdefault("staging_directory", "staging")

        def validate_internal_relative(value: Any, label: str) -> None:
            p = Path(str(value))
            if p.is_absolute() or any(part == ".." for part in p.parts):
                raise ConfigError(f"{label} must be a relative path without '..': {value}")

        validate_internal_relative(backup["deleted_directory"], "backup.deleted_directory")
        validate_internal_relative(internal["directory"], "internal.directory")
        validate_internal_relative(internal["staging_directory"], "internal.staging_directory")

        options = cfg.setdefault("options", {})
        options.setdefault("hash_algorithm", "sha256")
        options.setdefault("stop_on_error", False)
        options.setdefault("log_level", "INFO")

        logging_cfg = cfg.setdefault("logging", {})
        logging_cfg.setdefault("max_bytes", 5_000_000)
        logging_cfg.setdefault("backup_count", 5)

        deletion = cfg.setdefault("deletion", {})
        deletion.setdefault("enabled", True)
        deletion.setdefault("missing_runs", 2)
        deletion.setdefault("max_percent_per_run", 20)
        deletion.setdefault("max_items_per_run", 500)
        deletion.setdefault("min_items_for_percent", 10)

        defaults = cfg.setdefault("defaults", {})
        defaults.setdefault("retention", {})
        defaults.setdefault("storage", "filesystem")

        raw_git_cfg = cfg.get("git")
        if raw_git_cfg is None:
            git_cfg = {}
            cfg["git"] = git_cfg
        elif not isinstance(raw_git_cfg, dict):
            raise ConfigError("git must be a mapping")
        else:
            git_cfg = raw_git_cfg
        git_cfg.setdefault("mode", "direct")
        git_mode = str(git_cfg.get("mode") or "direct").lower()
        if "branch" not in git_cfg:
            git_cfg["branch"] = "configbackup/{hostname}" if git_mode == "pull_request" else "main"
        git_cfg.setdefault("base_branch", "auto")
        git_cfg.setdefault("remote_name", "origin")
        if "push" not in git_cfg:
            git_cfg["push"] = (git_mode == "pull_request")
        git_cfg.setdefault("auto_init", True)
        git_cfg.setdefault("include_hostname", backup.get("include_hostname", False))
        git_cfg.setdefault("path_prefix", "")
        git_cfg.setdefault("author_name", "ConfigBackup")
        git_cfg.setdefault("author_email", "configbackup@localhost")
        git_cfg.setdefault("worktree_root", "")
        git_cfg.setdefault("ignore", [])
        if isinstance(git_cfg.get("ignore"), str):
            git_cfg["ignore"] = [git_cfg["ignore"]]
        pr_cfg = git_cfg.setdefault("pull_request", {})
        pr_cfg.setdefault("enabled", git_mode == "pull_request")
        pr_cfg.setdefault("provider", "github")
        pr_cfg.setdefault("draft", False)
        pr_cfg.setdefault("title", "ConfigBackup: {hostname}")
        pr_cfg.setdefault("body", "Automated configuration snapshot for {hostname}.\n\nGenerated by ConfigBackup {version}.")
        pr_cfg.setdefault("reviewers", [])
        pr_cfg.setdefault("labels", [])
        if git_cfg.get("repository"):
            git_cfg["repository"] = expand_string(str(git_cfg["repository"]), variables)
        if git_cfg.get("remote_url"):
            git_cfg["remote_url"] = expand_string(str(git_cfg["remote_url"]), variables)

        policies = cfg.setdefault("retention_policies", {})
        if not isinstance(policies, dict):
            raise ConfigError("retention_policies must be a mapping")

        tasks = cfg.get("tasks") or []
        if not isinstance(tasks, list):
            raise ConfigError("tasks must be a list")
        names: set[str] = set()
        resolved_tasks: list[dict[str, Any]] = []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ConfigError(f"tasks[{index}] must be a mapping")
            resolved = self._resolve_task(task, cfg)
            name = resolved["name"]
            if name in names:
                raise ConfigError(f"Duplicate task name: {name}")
            names.add(name)
            resolved_tasks.append(resolved)
        cfg["tasks"] = resolved_tasks
        self._validate(cfg)
        return cfg

    def _resolve_task(self, task: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        result = deep_merge(cfg.get("defaults") or {}, task)
        name = str(result.get("name") or "").strip()
        if not name:
            raise ConfigError("Every task requires a non-empty name")
        result["name"] = name
        task_type = str(result.get("type") or "").lower()
        if task_type not in TASK_TYPES:
            raise ConfigError(f"Task {name!r} has invalid type {task_type!r}")
        result["type"] = task_type
        default_phase = "pre_backup" if task_type == "execute" else "backup"
        result.setdefault("phase", default_phase)
        result.setdefault("required", True)
        result.setdefault("depends_on", [])
        result.setdefault("run_on_failure", result["phase"] == "post_run")
        result.setdefault("stderr", "log")
        result.setdefault("timeout", None)
        result.setdefault("arguments", [])
        result.setdefault("environment", {})
        result.setdefault("storage", "filesystem")
        result["storage"] = str(result.get("storage") or "filesystem").lower()
        for field_name in ("include", "exclude"):
            if isinstance(result.get(field_name), str):
                result[field_name] = [result[field_name]]
            elif result.get(field_name) is not None and not isinstance(result.get(field_name), list):
                raise ConfigError(f"Task {name!r}: {field_name} must be a string or list")
        if not isinstance(result.get("arguments"), list):
            raise ConfigError(f"Task {name!r}: arguments must be a list")
        if not isinstance(result.get("environment"), dict):
            raise ConfigError(f"Task {name!r}: environment must be a mapping")
        if result.get("deletion") is not None and not isinstance(result.get("deletion"), dict):
            raise ConfigError(f"Task {name!r}: deletion must be a mapping")
        if task.get("retention") is not None and not isinstance(task.get("retention"), dict):
            raise ConfigError(f"Task {name!r}: retention must be a mapping")

        # If a task explicitly supplies retention without retention_policy, it is
        # intentionally opting out of the default named policy and defining its
        # retention locally.
        explicit_policy = task.get("retention_policy") if "retention_policy" in task else None
        if explicit_policy:
            policy_name = explicit_policy
        elif "retention" in task:
            policy_name = None
            result.pop("retention_policy", None)
        else:
            policy_name = cfg.get("defaults", {}).get("retention_policy")
        base_retention: dict[str, Any] = {}
        if policy_name:
            policies = cfg.get("retention_policies") or {}
            if policy_name not in policies:
                raise ConfigError(f"Task {name!r} references unknown retention policy {policy_name!r}")
            base_retention = copy.deepcopy(policies[policy_name])
        inline = task.get("retention") or {}
        result["retention"] = normalize_retention(deep_merge(base_retention, inline))
        return result

    def _validate(self, cfg: dict[str, Any]) -> None:
        try:
            hashlib.new(str(cfg.get("options", {}).get("hash_algorithm", "sha256")))
        except ValueError as exc:
            raise ConfigError(f"Unsupported hash algorithm: {cfg.get('options', {}).get('hash_algorithm')}") from exc
        log_level = str(cfg.get("options", {}).get("log_level", "INFO")).upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigError(f"Invalid log level: {log_level}")

        def validate_deletion_settings(settings: dict[str, Any], label: str) -> None:
            try:
                missing_runs = int(settings.get("missing_runs", 2))
                min_items = int(settings.get("min_items_for_percent", 10))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{label}: deletion counters must be integers") from exc
            if missing_runs < 1:
                raise ConfigError(f"{label}.missing_runs must be at least 1")
            if min_items < 1:
                raise ConfigError(f"{label}.min_items_for_percent must be at least 1")
            max_percent = settings.get("max_percent_per_run", 20)
            if max_percent is not None:
                try:
                    max_percent = float(max_percent)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"{label}.max_percent_per_run must be a number or null") from exc
                if not 0 <= max_percent <= 100:
                    raise ConfigError(f"{label}.max_percent_per_run must be between 0 and 100")
            max_items = settings.get("max_items_per_run", 500)
            if max_items is not None:
                try:
                    max_items = int(max_items)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"{label}.max_items_per_run must be an integer or null") from exc
                if max_items < 1:
                    raise ConfigError(f"{label}.max_items_per_run must be at least 1")

        validate_deletion_settings(cfg.get("deletion") or {}, "deletion")
        phase_index = {p: i for i, p in enumerate(PHASES)}
        tasks_by_name = {t["name"]: t for t in cfg["tasks"]}
        for task in cfg["tasks"]:
            name = task["name"]
            validate_deletion_settings(deep_merge(cfg.get("deletion") or {}, task.get("deletion") or {}), f"task {name!r}.deletion")
            if task["phase"] not in PHASES:
                raise ConfigError(f"Task {name!r}: invalid phase {task['phase']!r}")
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                deps = [deps]
                task["depends_on"] = deps
            if not isinstance(deps, list):
                raise ConfigError(f"Task {name!r}: depends_on must be a list or string")
            for dep in deps:
                if dep not in tasks_by_name:
                    raise ConfigError(f"Task {name!r}: unknown dependency {dep!r}")
                if phase_index[tasks_by_name[dep]["phase"]] > phase_index[task["phase"]]:
                    raise ConfigError(f"Task {name!r}: dependency {dep!r} is in a later phase")
            if task["type"] in {"file", "directory", "glob"}:
                if "source" not in task:
                    raise ConfigError(f"Task {name!r}: source is required")
                sources = task["source"] if isinstance(task["source"], list) else [task["source"]]
                if not sources or any(not str(src).strip() for src in sources):
                    raise ConfigError(f"Task {name!r}: source must contain at least one non-empty path")
            if task["type"] in {"execute", "command"}:
                if not str(task.get("executable") or "").strip():
                    raise ConfigError(f"Task {name!r}: executable is required")
            if task["type"] == "command" and not str(task.get("output") or "").strip():
                raise ConfigError(f"Task {name!r}: output is required for command tasks")
            if task.get("timeout") is not None:
                try:
                    timeout = float(task["timeout"])
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"Task {name!r}: timeout must be a positive number") from exc
                if timeout <= 0:
                    raise ConfigError(f"Task {name!r}: timeout must be greater than zero")
                task["timeout"] = timeout
            if task.get("stderr") not in {"log", "discard", "capture", "merge"}:
                raise ConfigError(f"Task {name!r}: stderr must be log, discard, capture, or merge")
            storage = str(task.get("storage", "filesystem")).lower()
            if storage not in {"filesystem", "git", "both"}:
                raise ConfigError(f"Task {name!r}: storage must be filesystem, git, or both")
            if task["type"] == "execute" and storage != "filesystem":
                raise ConfigError(f"Task {name!r}: execute tasks do not archive artifacts; storage must remain filesystem")
            if storage in {"git", "both"} and not str((cfg.get("git") or {}).get("repository") or "").strip():
                raise ConfigError(f"Task {name!r}: storage={storage} requires git.repository")
        git_cfg = cfg.get("git") or {}
        if git_cfg.get("repository"):
            prefix = Path(str(git_cfg.get("path_prefix") or ""))
            if prefix.is_absolute() or any(part == ".." for part in prefix.parts):
                raise ConfigError("git.path_prefix must be relative and may not contain '..'")
            mode = str(git_cfg.get("mode") or "direct").lower()
            if mode not in {"direct", "pull_request"}:
                raise ConfigError("git.mode must be direct or pull_request")
            if not str(git_cfg.get("branch") or "").strip():
                raise ConfigError("git.branch must be non-empty")
            if not str(git_cfg.get("remote_name") or "").strip():
                raise ConfigError("git.remote_name must be non-empty")
            ignore_patterns = git_cfg.get("ignore") or []
            if not isinstance(ignore_patterns, list):
                raise ConfigError("git.ignore must be a string or list of glob patterns")
            cleaned_ignore: list[str] = []
            for pattern in ignore_patterns:
                value = str(pattern).strip()
                if not value:
                    continue
                if "\n" in value or "\r" in value:
                    raise ConfigError("git.ignore patterns may not contain newlines")
                if value.startswith("!"):
                    raise ConfigError("git.ignore negation patterns are not supported")
                cleaned_ignore.append(value.replace("\\", "/"))
            git_cfg["ignore"] = cleaned_ignore
            if mode == "pull_request":
                if not bool(git_cfg.get("push")):
                    raise ConfigError("git.mode=pull_request requires git.push=true")
                if not str(git_cfg.get("base_branch") or "").strip():
                    raise ConfigError("git.base_branch must be non-empty")
                pr_cfg = git_cfg.get("pull_request") or {}
                provider = str(pr_cfg.get("provider") or "github").lower()
                if bool(pr_cfg.get("enabled")) and provider != "github":
                    raise ConfigError("git.pull_request.provider currently supports only 'github'")
                for list_name in ("reviewers", "labels"):
                    value = pr_cfg.get(list_name) or []
                    if not isinstance(value, list):
                        raise ConfigError(f"git.pull_request.{list_name} must be a list")
            remote_url = str(git_cfg.get("remote_url") or "").strip()
            if remote_url and re.match(r"(?i)^https?://[^/]*@", remote_url):
                raise ConfigError(
                    "git.remote_url may not embed HTTP(S) user information or credentials; "
                    "use Git Credential Manager, SSH, a deploy key, or another normal Git credential mechanism"
                )
        self._validate_dependencies(tasks_by_name)
        for policy_name, policy in cfg.get("retention_policies", {}).items():
            normalize_retention(policy, label=f"retention_policies.{policy_name}")

    def _validate_dependencies(self, tasks: dict[str, dict[str, Any]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(name: str) -> None:
            if name in visiting:
                raise ConfigError(f"Dependency cycle detected involving {name!r}")
            if name in visited:
                return
            visiting.add(name)
            for dep in tasks[name].get("depends_on") or []:
                walk(dep)
            visiting.remove(name)
            visited.add(name)

        for name in tasks:
            walk(name)


def normalize_retention(value: dict[str, Any] | None, label: str = "retention") -> dict[str, Any]:
    if value is not None and not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    value = copy.deepcopy(value or {})
    # Backward-friendly shorthand: if active/deleted omitted, interpret as active.
    if "active" not in value and "deleted" not in value:
        value = {"active": value}
    active = value.setdefault("active", {})
    deleted = value.setdefault("deleted", {})
    if not active:
        active.update({"mode": "indefinite", "min_versions": 3})
    if not deleted:
        deleted.update({"mode": "indefinite", "min_versions": active.get("min_versions", 3), "grace_days": 30})
    for section_name, section in [("active", active), ("deleted", deleted)]:
        section.setdefault("mode", "indefinite")
        section.setdefault("min_versions", 3)
        mode = section["mode"]
        if mode not in {"indefinite", "simple", "tiered"}:
            raise ConfigError(f"{label}.{section_name}: invalid mode {mode!r}")
        if int(section.get("min_versions", 0)) < 1:
            raise ConfigError(f"{label}.{section_name}: min_versions must be at least 1")
        section["min_versions"] = int(section["min_versions"])
        if "max_versions" in section and section["max_versions"] is not None:
            section["max_versions"] = int(section["max_versions"])
            if section["max_versions"] < section["min_versions"]:
                raise ConfigError(f"{label}.{section_name}: max_versions cannot be less than min_versions")
        if "max_age_days" in section and section["max_age_days"] is not None:
            section["max_age_days"] = int(section["max_age_days"])
            if section["max_age_days"] < 0:
                raise ConfigError(f"{label}.{section_name}: max_age_days cannot be negative")
        if "max_size" in section:
            section["max_size_bytes"] = parse_size(section.get("max_size"))
        if section_name == "deleted":
            section["grace_days"] = int(section.get("grace_days", 30))
            if section["grace_days"] < 0:
                raise ConfigError(f"{label}.deleted.grace_days cannot be negative")
            if section.get("purge_after_days") is not None:
                section["purge_after_days"] = int(section["purge_after_days"])
                if section["purge_after_days"] < 0:
                    raise ConfigError(f"{label}.deleted.purge_after_days cannot be negative")
        if mode == "tiered":
            tiers = section.get("tiers")
            if not isinstance(tiers, list) or not tiers:
                raise ConfigError(f"{label}.{section_name}: tiered mode requires non-empty tiers")
            forever_seen = False
            for i, tier in enumerate(tiers):
                if not isinstance(tier, dict):
                    raise ConfigError(f"{label}.{section_name}.tiers[{i}] must be a mapping")
                interval = tier.get("interval") or tier.get("keep")
                if interval not in INTERVALS:
                    raise ConfigError(f"{label}.{section_name}.tiers[{i}]: invalid interval {interval!r}")
                tier["interval"] = interval
                if tier.get("forever"):
                    if i != len(tiers) - 1:
                        raise ConfigError(f"{label}.{section_name}: forever tier must be last")
                    forever_seen = True
                else:
                    if forever_seen:
                        raise ConfigError(f"{label}.{section_name}: no tier may follow forever")
                    if "duration_days" not in tier:
                        # Accept earlier spelling.
                        if "for_days" in tier:
                            tier["duration_days"] = tier["for_days"]
                        else:
                            raise ConfigError(f"{label}.{section_name}.tiers[{i}]: duration_days or forever is required")
                    tier["duration_days"] = int(tier["duration_days"])
                    if tier["duration_days"] <= 0:
                        raise ConfigError(f"{label}.{section_name}.tiers[{i}]: duration_days must be > 0")
    return value


class StateStore:
    def __init__(self, path: Path, dry_run: bool = False):
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")
        self.dry_run = dry_run
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": STATE_VERSION, "tasks": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read state file {self.path}: {exc}") from exc
        if data.get("version") != STATE_VERSION:
            raise ConfigError(f"Unsupported state version {data.get('version')}; expected {STATE_VERSION}")
        data.setdefault("tasks", {})
        return data

    def save(self) -> None:
        if self.dry_run:
            return
        if self.path.exists():
            ensure_parent(self.backup_path)
            shutil.copy2(self.path, self.backup_path)
        atomic_json_write(self.path, self.data)

    def task(self, name: str) -> dict[str, Any]:
        task = self.data["tasks"].setdefault(name, {})
        task.setdefault("files", {})
        task.setdefault("last_success", None)
        return task


class BackupEngine:
    def __init__(self, cfg: dict[str, Any], *, dry_run: bool = False, prune_only: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.prune_only = prune_only
        self.start_time = now_local()
        self.run_id = self.start_time.strftime("%Y%m%d-%H%M%S-%f")
        self.hostname = str(cfg["backup"].get("hostname") or socket.gethostname())
        self.root = Path(cfg["backup"]["root"]).expanduser().absolute()
        if cfg["backup"].get("include_hostname"):
            self.archive_root = self.root / sanitize_component(self.hostname)
        else:
            self.archive_root = self.root
        self.internal_root = self.archive_root / cfg["internal"]["directory"]
        staging_dir = cfg["internal"].get("staging_root")
        if staging_dir:
            self.staging_root = Path(expand_string(str(staging_dir), cfg.get("variables") or {})).absolute()
        else:
            # Keep staging outside the archive by default so generated collector
            # output can safely be used as a backup source without defeating
            # recursion protection. The root hash isolates multiple configs.
            root_tag = hashlib.sha256(str(self.archive_root).encode("utf-8")).hexdigest()[:12]
            self.staging_root = Path(tempfile.gettempdir()) / "configbackup" / root_tag / cfg["internal"]["staging_directory"]
        self.deleted_root = self.archive_root / cfg["backup"]["deleted_directory"]
        root_resolved = self.root.resolve(strict=False)
        if root_resolved == Path(self.root.anchor).resolve(strict=False):
            raise ConfigError(f"backup.root may not be a filesystem/drive root: {self.root}")
        staging_resolved = self.staging_root.resolve(strict=False)
        try:
            staging_resolved.relative_to(root_resolved)
        except ValueError:
            pass
        else:
            if not staging_dir:
                # Rare case: backup.root itself contains the OS temp directory.
                # Use a sibling staging location instead.
                self.staging_root = self.root.parent / f".configbackup-staging-{root_tag}"
            else:
                raise ConfigError(
                    f"internal.staging_root must be outside backup.root to avoid archive recursion: {self.staging_root}"
                )
        self.state = StateStore(self.internal_root / "state.json", dry_run=dry_run)
        self.stats = RunStats()
        self.results: dict[str, TaskResult] = {}
        self.logger = self._setup_logging()
        self.hash_algorithm = cfg["options"]["hash_algorithm"]
        self._written_paths: set[Path] = set()
        self._logical_owners: dict[str, tuple[str, str]] = {}
        self._uses_git = any(
            t.get("type") != "execute" and str(t.get("storage", "filesystem")).lower() in {"git", "both"}
            for t in cfg.get("tasks", [])
        )
        self.git_cfg = cfg.get("git") or {}
        self.git_mode = str(self.git_cfg.get("mode") or "direct").lower()
        self.git_control_repo: Path | None = None
        self.git_repo: Path | None = None
        self.git_worktree: Path | None = None
        self.git_snapshot_root: Path | None = None
        self.git_base_branch: str | None = None
        self.git_branch: str | None = None
        self.git_open_pr_url: str | None = None
        self.git_force_push = False
        if self._uses_git:
            self.git_control_repo = Path(str(self.git_cfg["repository"])).expanduser().absolute()
            if self.git_mode == "pull_request":
                branch_hint = self._format_git_template(str(self.git_cfg.get("branch") or "configbackup/{hostname}"))
                worktree_root = str(self.git_cfg.get("worktree_root") or "").strip()
                if worktree_root:
                    base_worktree = Path(expand_string(worktree_root, cfg.get("variables") or {})).expanduser().absolute()
                else:
                    repo_tag = hashlib.sha256(str(self.git_control_repo).encode("utf-8")).hexdigest()[:12]
                    base_worktree = self.staging_root.parent / "git-worktrees" / repo_tag
                branch_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", branch_hint).strip("._-") or "configbackup"
                self.git_worktree = base_worktree / branch_tag
                self.git_repo = self.git_worktree
            else:
                self.git_repo = self.git_control_repo
            prefix = Path(str(self.git_cfg.get("path_prefix") or ""))
            if self.git_cfg.get("include_hostname"):
                prefix = prefix / sanitize_component(self.hostname)
            self.git_snapshot_root = self.git_repo / prefix

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger(f"configbackup.{id(self)}")
        logger.setLevel(getattr(logging, str(self.cfg["options"].get("log_level", "INFO")).upper(), logging.INFO))
        logger.propagate = False
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)
        if not self.dry_run:
            self.internal_root.mkdir(parents=True, exist_ok=True)
            log_path = self.internal_root / "configbackup.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=int(self.cfg["logging"].get("max_bytes", 5_000_000)),
                backupCount=int(self.cfg["logging"].get("backup_count", 5)),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        return logger

    def variables_for_task(self, task: dict[str, Any]) -> dict[str, str]:
        variables = dict(self.cfg.get("variables") or {})
        task_staging = self.staging_root / sanitize_component(task["name"])
        variables.update(
            {
                "CONFIGBACKUP_ROOT": str(self.root),
                "CONFIGBACKUP_ARCHIVE_ROOT": str(self.archive_root),
                "CONFIGBACKUP_STAGING": str(self.staging_root),
                "CONFIGBACKUP_TASK_NAME": task["name"],
                "CONFIGBACKUP_RUN_ID": self.run_id,
                "CONFIGBACKUP_DATE": self.start_time.strftime("%Y%m%d"),
                "CONFIGBACKUP_HOSTNAME": self.hostname,
                "CONFIGBACKUP_GIT_ROOT": str(self.git_repo) if self.git_repo is not None else "",
            }
        )
        output_dir = task.get("output_directory")
        if output_dir:
            variables["CONFIGBACKUP_OUTPUT"] = expand_string(str(output_dir), variables, strict=True)
        else:
            variables["CONFIGBACKUP_OUTPUT"] = str(task_staging)
        return variables

    def resolve_task_runtime(self, task: dict[str, Any]) -> dict[str, Any]:
        return expand_value(task, self.variables_for_task(task), strict=True)

    def run(self) -> int:
        if not self.dry_run:
            self.root.mkdir(parents=True, exist_ok=True)
            self.internal_root.mkdir(parents=True, exist_ok=True)
            self.staging_root.mkdir(parents=True, exist_ok=True)
        self.logger.info("%s %s run %s started%s", APP_NAME, APP_VERSION, self.run_id, " (dry-run)" if self.dry_run else "")
        if self._uses_git and not self.prune_only:
            self._prepare_git_repo()
        if self.prune_only:
            self._run_retention_all()
            self.state.save()
            self._write_run_manifest()
            self._print_summary()
            self._close_logging()
            return 0

        tasks_by_phase: dict[str, list[dict[str, Any]]] = {p: [] for p in PHASES}
        for task in self.cfg["tasks"]:
            tasks_by_phase[task["phase"]].append(task)

        abort_normal_tasks = False
        for phase in PHASES:
            pending = list(tasks_by_phase[phase])
            while pending:
                progressed = False
                for task in list(pending):
                    same_phase_deps = [
                        d for d in task.get("depends_on", [])
                        if any(t["name"] == d for t in tasks_by_phase[phase])
                    ]
                    if any(dep not in self.results for dep in same_phase_deps):
                        continue
                    pending.remove(task)
                    progressed = True
                    if abort_normal_tasks and not (phase == "post_run" and task.get("run_on_failure", True)):
                        self.results[task["name"]] = TaskResult(
                            task["name"], "skipped", "stop_on_error", bool(task.get("required", True))
                        )
                        continue
                    result = self._run_task(task)
                    self.results[task["name"]] = result
                    if result.status == "failed" and self.cfg["options"].get("stop_on_error") and result.required:
                        abort_normal_tasks = True
                        self.logger.error("stop_on_error=true: remaining normal tasks will be skipped; post_run cleanup may still run")
                if not progressed:
                    unresolved = ", ".join(t["name"] for t in pending)
                    raise ConfigError(f"Could not resolve task ordering in phase {phase}: {unresolved}")

        preliminary_required_problems = [
            r for r in self.results.values()
            if r.required and r.status in {"failed", "skipped"}
        ]
        if self._uses_git and not self.dry_run:
            if preliminary_required_problems:
                self.logger.warning("Git snapshot changes rolled back because a required task failed or was skipped")
                self._rollback_git_repo()
            else:
                try:
                    self._finalize_git_repo()
                except Exception as exc:
                    self.logger.exception("Git commit/push failed: %s", exc)
                    self.results["git-finalize"] = TaskResult(
                        "git-finalize", "failed", str(exc), True, started=iso_now(), ended=iso_now()
                    )

        if self.dry_run:
            required_problems: list[TaskResult] = []
            eligible_retention = {name for name, r in self.results.items() if r.status in {"success", "dry-run"}}
            self._run_retention_all(eligible_retention)
        else:
            required_problems = [
                r for r in self.results.values()
                if r.required and r.status in {"failed", "skipped"}
            ]
            if required_problems:
                self.logger.warning("Retention skipped because one or more required tasks failed or were skipped")
            else:
                eligible_retention = {name for name, r in self.results.items() if r.status == "success"}
                self._run_retention_all(eligible_retention)
        self.state.save()
        self._write_run_manifest()
        self._print_summary()
        exit_code = 4 if required_problems else 0
        self._close_logging()
        return exit_code

    def _format_git_template(self, value: str) -> str:
        mapping = {
            "hostname": sanitize_component(self.hostname),
            "date": self.start_time.strftime("%Y-%m-%d"),
            "run_id": self.run_id,
            "version": APP_VERSION,
        }
        try:
            return value.format(**mapping)
        except KeyError as exc:
            raise ConfigError(f"Unknown Git template placeholder {exc.args[0]!r} in {value!r}") from exc

    def _git_command(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.git_repo is None:
            raise RuntimeError("Git repository/worktree is not configured")
        cmd = ["git", "-C", str(self.git_repo), *args]
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Git command failed ({completed.returncode}): {' '.join(cmd)}: {detail}")
        return completed

    def _git_control_command(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        repo = self.git_control_repo or self.git_repo
        if repo is None:
            raise RuntimeError("Git control repository is not configured")
        cmd = ["git", "-C", str(repo), *args]
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Git command failed ({completed.returncode}): {' '.join(cmd)}: {detail}")
        return completed

    def _gh_command(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which("gh") is None:
            raise ConfigError(
                "GitHub pull-request automation requires the GitHub CLI ('gh') on PATH. "
                "Install it and authenticate the same OS account that runs ConfigBackup."
            )
        cwd = self.git_repo if self.git_repo is not None and self.git_repo.exists() else self.git_control_repo
        completed = subprocess.run(
            ["gh", *args], cwd=str(cwd) if cwd else None, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"GitHub CLI failed ({completed.returncode}): gh {' '.join(args)}: {detail}")
        return completed

    def _configure_git_remote(self) -> None:
        remote_url = str(self.git_cfg.get("remote_url") or "").strip()
        if not remote_url:
            return
        remote = str(self.git_cfg.get("remote_name") or "origin")
        current_remote = self._git_control_command(["remote", "get-url", remote], check=False)
        if current_remote.returncode != 0:
            self._git_control_command(["remote", "add", remote, remote_url])
        elif current_remote.stdout.strip() != remote_url:
            raise ConfigError(
                f"Git remote {remote!r} already points to {current_remote.stdout.strip()!r}, "
                f"not configured remote_url {remote_url!r}"
            )

    def _detect_git_base_branch(self) -> str:
        configured = str(self.git_cfg.get("base_branch") or "auto").strip()
        if configured.lower() != "auto":
            return configured
        remote = str(self.git_cfg.get("remote_name") or "origin")
        result = self._git_control_command(["ls-remote", "--symref", remote, "HEAD"], check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.match(r"^ref:\s+refs/heads/(.+)\s+HEAD$", line.strip())
                if match:
                    return match.group(1)
        # Fallback to the locally known remote HEAD after fetch/set-head.
        self._git_control_command(["remote", "set-head", remote, "-a"], check=False)
        sym = self._git_control_command(["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"], check=False)
        if sym.returncode == 0 and "/" in sym.stdout.strip():
            return sym.stdout.strip().split("/", 1)[1]
        raise ConfigError(
            f"Unable to determine the default branch for remote {remote!r}. "
            "Set git.base_branch explicitly (for example, main or master)."
        )

    def _github_find_open_pr(self) -> dict[str, Any] | None:
        pr_cfg = self.git_cfg.get("pull_request") or {}
        if not bool(pr_cfg.get("enabled")):
            return None
        assert self.git_branch and self.git_base_branch
        result = self._gh_command([
            "pr", "list",
            "--head", self.git_branch,
            "--base", self.git_base_branch,
            "--state", "open",
            "--json", "number,url",
            "--limit", "1",
        ])
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Unable to parse GitHub CLI PR response: {result.stdout!r}") from exc
        if items:
            return dict(items[0])
        return None

    def _cleanup_git_worktree(self) -> None:
        if self.git_mode != "pull_request" or self.git_worktree is None or self.git_control_repo is None:
            return
        if not self.git_control_repo.exists():
            return
        # Git may know about the worktree even if its directory is already gone.
        self._git_control_command(["worktree", "remove", "--force", str(self.git_worktree)], check=False)
        self._git_control_command(["worktree", "prune"], check=False)
        if self.git_worktree.exists():
            shutil.rmtree(self.git_worktree, ignore_errors=True)

    def _prepare_git_pull_request_repo(self) -> None:
        assert self.git_control_repo is not None and self.git_worktree is not None
        repo = self.git_control_repo
        if not repo.exists():
            raise ConfigError(f"git.repository must be an existing Git working tree in pull_request mode: {repo}")
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
            raise ConfigError(f"git.repository is not a Git working tree: {repo}")

        self._configure_git_remote()
        remote = str(self.git_cfg.get("remote_name") or "origin")
        remote_probe = self._git_control_command(["remote", "get-url", remote], check=False)
        if remote_probe.returncode != 0:
            raise ConfigError(f"git.mode=pull_request requires configured remote {remote!r}")

        if self.dry_run:
            self.logger.info("DRY-RUN: would use isolated Git worktree %s from %s", self.git_worktree, repo)
            return

        self._git_control_command(["fetch", remote, "--prune"])
        self.git_base_branch = self._detect_git_base_branch()
        branch_template = str(self.git_cfg.get("branch") or "configbackup/{hostname}")
        self.git_branch = self._format_git_template(branch_template)
        check_branch = self._git_control_command(["check-ref-format", "--branch", self.git_branch], check=False)
        if check_branch.returncode != 0:
            raise ConfigError(f"Invalid git.branch after template expansion: {self.git_branch!r}")

        base_ref = f"refs/remotes/{remote}/{self.git_base_branch}"
        if self._git_control_command(["show-ref", "--verify", base_ref], check=False).returncode != 0:
            raise ConfigError(f"Remote base branch {remote}/{self.git_base_branch} was not found after fetch")

        pr_cfg = self.git_cfg.get("pull_request") or {}
        if bool(pr_cfg.get("enabled")):
            # Fail early before changing branch/worktree state if GitHub CLI is unavailable or unauthenticated.
            self._gh_command(["auth", "status"], check=True)
            open_pr = self._github_find_open_pr()
        else:
            open_pr = None
        if open_pr:
            self.git_open_pr_url = str(open_pr.get("url") or "") or None

        self._cleanup_git_worktree()
        self.git_worktree.parent.mkdir(parents=True, exist_ok=True)

        remote_branch_ref = f"refs/remotes/{remote}/{self.git_branch}"
        remote_branch_exists = self._git_control_command(["show-ref", "--verify", remote_branch_ref], check=False).returncode == 0
        local_branch_ref = f"refs/heads/{self.git_branch}"
        local_branch_exists = self._git_control_command(["show-ref", "--verify", local_branch_ref], check=False).returncode == 0

        if open_pr:
            if not remote_branch_exists:
                raise RuntimeError(
                    f"GitHub reports an open PR for {self.git_branch!r}, but {remote}/{self.git_branch} was not found"
                )
            start_ref = f"{remote}/{self.git_branch}"
            self.git_force_push = False
        else:
            start_ref = f"{remote}/{self.git_base_branch}"
            # A leftover remote automation branch may belong to a merged/closed PR. Reset it to current base.
            self.git_force_push = remote_branch_exists

        if local_branch_exists:
            reset = self._git_control_command(["branch", "-f", self.git_branch, start_ref], check=False)
            if reset.returncode != 0:
                detail = reset.stderr.strip() or reset.stdout.strip()
                raise RuntimeError(
                    f"Unable to reset ConfigBackup branch {self.git_branch!r}. "
                    f"Make sure it is not checked out in another worktree. {detail}"
                )
        else:
            self._git_control_command(["branch", self.git_branch, start_ref])

        self._git_control_command(["worktree", "add", str(self.git_worktree), self.git_branch])
        self.git_repo = self.git_worktree
        prefix = Path(str(self.git_cfg.get("path_prefix") or ""))
        if self.git_cfg.get("include_hostname"):
            prefix = prefix / sanitize_component(self.hostname)
        self.git_snapshot_root = self.git_repo / prefix
        self.git_snapshot_root.mkdir(parents=True, exist_ok=True)

        author_name = str(self.git_cfg.get("author_name") or "ConfigBackup")
        author_email = str(self.git_cfg.get("author_email") or "configbackup@localhost")
        self._git_command(["config", "user.name", author_name])
        self._git_command(["config", "user.email", author_email])
        self.logger.info(
            "Git PR mode: branch %s based on %s/%s in isolated worktree %s",
            self.git_branch, remote, self.git_base_branch, self.git_worktree,
        )
        if self.git_open_pr_url:
            self.logger.info("GitHub PR already open: %s", self.git_open_pr_url)

    def _prepare_git_direct_repo(self) -> None:
        assert self.git_control_repo is not None
        self.git_repo = self.git_control_repo
        repo = self.git_repo
        if self.dry_run:
            if repo.exists() and not (repo / ".git").exists():
                raise ConfigError(f"git.repository exists but is not a Git working tree: {repo}")
            if not repo.exists() and not self.git_cfg.get("auto_init", True):
                raise ConfigError(f"git.repository does not exist and git.auto_init=false: {repo}")
            self.logger.info("DRY-RUN: Git snapshot repository %s", repo)
            return

        if not repo.exists():
            if not self.git_cfg.get("auto_init", True):
                raise ConfigError(f"git.repository does not exist and git.auto_init=false: {repo}")
            repo.mkdir(parents=True, exist_ok=True)
            init = subprocess.run(["git", "init", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if init.returncode != 0:
                raise RuntimeError(init.stderr.strip() or "git init failed")
        elif not (repo / ".git").exists():
            if self.git_cfg.get("auto_init", True) and not any(repo.iterdir()):
                init = subprocess.run(["git", "init", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                if init.returncode != 0:
                    raise RuntimeError(init.stderr.strip() or "git init failed")
            else:
                raise ConfigError(f"git.repository exists but is not a Git working tree: {repo}")

        status = self._git_command(["status", "--porcelain"], check=True).stdout.strip()
        if status:
            raise ConfigError(
                f"Git repository has pre-existing uncommitted changes: {repo}. "
                "Direct Git mode requires a clean working tree. Use git.mode=pull_request for an existing/shared repo."
            )

        author_name = str(self.git_cfg.get("author_name") or "ConfigBackup")
        author_email = str(self.git_cfg.get("author_email") or "configbackup@localhost")
        self._git_command(["config", "user.name", author_name])
        self._git_command(["config", "user.email", author_email])

        branch = str(self.git_cfg.get("branch") or "main")
        has_head = self._git_command(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0
        if has_head:
            current = self._git_command(["branch", "--show-current"], check=True).stdout.strip()
            if current != branch:
                exists = self._git_command(["show-ref", "--verify", f"refs/heads/{branch}"], check=False).returncode == 0
                self._git_command(["checkout", branch] if exists else ["checkout", "-b", branch])
        else:
            self._git_command(["checkout", "-B", branch])

        self._configure_git_remote()
        if self.git_snapshot_root is not None:
            self.git_snapshot_root.mkdir(parents=True, exist_ok=True)

    def _prepare_git_repo(self) -> None:
        if shutil.which("git") is None:
            raise ConfigError("Git-backed tasks require the git executable to be installed and on PATH")
        if self.git_mode == "pull_request":
            self._prepare_git_pull_request_repo()
        else:
            self._prepare_git_direct_repo()
        self._sync_gitignore()

    def _git_relative_path(self, logical: Path) -> Path:
        assert self.git_repo is not None and self.git_snapshot_root is not None
        path = self.git_snapshot_root / logical
        resolved_parent = path.parent.resolve(strict=False)
        repo_resolved = self.git_repo.resolve(strict=False)
        if not self._is_within(resolved_parent, repo_resolved):
            raise RuntimeError(f"Git destination escapes repository: {logical}")
        return path.relative_to(self.git_repo)

    def _git_ignore_patterns(self) -> list[str]:
        return [str(x) for x in (self.git_cfg.get("ignore") or []) if str(x).strip()]

    def _git_is_ignored(self, logical: Path) -> bool:
        return path_matches(posix_rel(logical), self._git_ignore_patterns())

    def _sync_gitignore(self) -> None:
        if self.git_repo is None or self.git_snapshot_root is None:
            return
        patterns = self._git_ignore_patterns()
        gitignore = self.git_snapshot_root / ".gitignore"
        begin = "# BEGIN ConfigBackup managed ignores"
        end = "# END ConfigBackup managed ignores"
        existing = ""
        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8", errors="replace")
        # Preserve any user-maintained lines outside our marker block.
        block_re = re.compile(
            rf"(?ms)^{re.escape(begin)}\n.*?^{re.escape(end)}(?:\n|$)"
        )
        preserved = block_re.sub("", existing).rstrip("\n")
        pieces: list[str] = []
        if preserved:
            pieces.append(preserved)
        if patterns:
            managed = "\n".join([begin, *patterns, end])
            pieces.append(managed)
        content = "\n\n".join(pieces)
        if content:
            content += "\n"
        if self.dry_run:
            self.logger.info("DRY-RUN: would synchronize Git ignore rules at %s", gitignore)
            return
        if content:
            ensure_parent(gitignore)
            gitignore.write_text(content, encoding="utf-8", newline="\n")
        elif gitignore.exists() and not preserved:
            gitignore.unlink()

        if not patterns:
            return
        # .gitignore does not untrack files that are already in Git. Remove any
        # currently tracked managed files that now match ConfigBackup's Git-only ignores.
        try:
            managed_rel = self.git_snapshot_root.relative_to(self.git_repo)
        except ValueError:
            return
        managed_arg = managed_rel.as_posix() if managed_rel.parts else "."
        listed = self._git_command(["ls-files", "--", managed_arg], check=False)
        if listed.returncode != 0:
            return
        for line in listed.stdout.splitlines():
            rel_repo = Path(line.strip())
            if not line.strip():
                continue
            try:
                rel_snapshot = rel_repo.relative_to(managed_rel) if managed_rel.parts else rel_repo
            except ValueError:
                continue
            if rel_snapshot.as_posix() == ".gitignore":
                continue
            if not path_matches(rel_snapshot.as_posix(), patterns):
                continue
            candidate = self.git_repo / rel_repo
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink(missing_ok=True)
                self._remove_empty_git_parents(candidate.parent)
                self.logger.info("GIT IGNORE removed previously tracked snapshot %s", rel_repo.as_posix())

    def _git_backup_one_file(
        self,
        task: dict[str, Any],
        src: Path,
        logical: Path,
        source_display: str | None = None,
        *,
        count_stats: bool = True,
    ) -> str:
        if self.git_repo is None:
            raise RuntimeError("Git storage requested but git.repository is not configured")
        task_state = self.state.task(task["name"])
        if self._git_is_ignored(logical):
            key = posix_rel(logical)
            rel_git = self._git_relative_path(logical)
            destination = self.git_repo / rel_git
            file_state = task_state["files"].get(key)
            if self.dry_run:
                self.logger.info("DRY-RUN: Git ignore would skip %s", source_display or src)
            else:
                if destination.exists() or destination.is_symlink():
                    destination.unlink(missing_ok=True)
                    self._remove_empty_git_parents(destination.parent)
                    self.logger.info("GIT IGNORE %s (removed current Git snapshot)", source_display or src)
                else:
                    self.logger.debug("GIT IGNORE %s", source_display or src)
            if file_state is not None:
                file_state["git_hash"] = None
                file_state["git_path"] = None
                file_state["git_size"] = None
                file_state["git_ignored"] = True
            return "ignored"
        key = posix_rel(logical)
        source_identity = source_display or str(src.absolute())
        owner = self._logical_owners.get(key)
        if owner is not None and owner != (task["name"], source_identity):
            raise RuntimeError(
                f"Archive logical-path collision for {key!r}: "
                f"{owner[0]} ({owner[1]}) and {task['name']} ({source_identity})"
            )
        self._logical_owners[key] = (task["name"], source_identity)
        file_state = task_state["files"].setdefault(
            key,
            {"missing_runs": 0, "versions": [], "deleted_generations": [], "active": True},
        )
        file_state.setdefault("versions", [])
        file_state.setdefault("deleted_generations", [])
        file_state["git_ignored"] = False
        file_state["active"] = True
        file_state["missing_runs"] = 0
        file_state["last_seen"] = iso_now()
        src_hash = hash_file(src, self.hash_algorithm)
        size = src.stat().st_size
        rel_git = self._git_relative_path(logical)
        destination = self.git_repo / rel_git
        existed = destination.exists()
        same = existed and destination.is_file() and hash_file(destination, self.hash_algorithm) == src_hash
        if same:
            file_state["git_hash"] = src_hash
            file_state["git_path"] = rel_git.as_posix()
            if count_stats:
                self.stats.unchanged += 1
            self.logger.debug("GIT UNCHANGED %s", source_display or src)
            return "unchanged"
        status = "changed" if existed or file_state.get("git_hash") else "new"
        if self.dry_run:
            self.logger.info("DRY-RUN: would update Git snapshot %s -> %s", source_display or src, destination)
        else:
            atomic_copy(src, destination)
            if hash_file(destination, self.hash_algorithm) != src_hash:
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Git snapshot copy verification failed: {source_display or src}")
            self.logger.info("GIT %s %s -> %s", status.upper(), source_display or src, destination)
        file_state["git_hash"] = src_hash
        file_state["git_path"] = rel_git.as_posix()
        file_state["git_size"] = size
        file_state["git_updated"] = iso_now()
        if count_stats:
            if status == "new":
                self.stats.new += 1
            else:
                self.stats.changed += 1
            self.stats.stored += 1
            self.stats.bytes_written += size
        return status

    def _store_one_file(self, task: dict[str, Any], src: Path, logical: Path, source_display: str | None = None) -> str:
        storage = str(task.get("storage", "filesystem")).lower()
        statuses: list[str] = []
        size = src.stat().st_size
        if storage in {"filesystem", "both"}:
            statuses.append(self._backup_one_file(task, src, logical, source_display=source_display, count_stats=False))
        if storage in {"git", "both"}:
            statuses.append(self._git_backup_one_file(task, src, logical, source_display=source_display, count_stats=False))
        if not statuses:
            raise RuntimeError(f"No storage backend selected for task {task['name']}")
        effective_statuses = [x for x in statuses if x != "ignored"]
        if not effective_statuses:
            return "ignored"
        changed_statuses = [x for x in effective_statuses if x != "unchanged"]
        if not changed_statuses:
            combined = "unchanged"
            self.stats.unchanged += 1
        elif all(x == "new" for x in changed_statuses):
            combined = "new"
            self.stats.new += 1
        else:
            combined = "changed"
            self.stats.changed += 1
        stored_count = sum(1 for x in effective_statuses if x != "unchanged")
        self.stats.stored += stored_count
        self.stats.bytes_written += size * stored_count
        return combined

    def _ensure_github_pull_request(self) -> str | None:
        pr_cfg = self.git_cfg.get("pull_request") or {}
        if not bool(pr_cfg.get("enabled")):
            return None
        assert self.git_branch and self.git_base_branch
        existing = self._github_find_open_pr()
        if existing:
            url = str(existing.get("url") or "") or None
            if url:
                self.logger.info("GitHub PR updated: %s", url)
            return url

        title = self._format_git_template(str(pr_cfg.get("title") or "ConfigBackup: {hostname}"))
        body = self._format_git_template(str(pr_cfg.get("body") or "Automated configuration snapshot for {hostname}."))
        args = [
            "pr", "create",
            "--base", self.git_base_branch,
            "--head", self.git_branch,
            "--title", title,
            "--body", body,
        ]
        if bool(pr_cfg.get("draft")):
            args.append("--draft")
        for reviewer in pr_cfg.get("reviewers") or []:
            args.extend(["--reviewer", str(reviewer)])
        for label in pr_cfg.get("labels") or []:
            args.extend(["--label", str(label)])
        created = self._gh_command(args)
        url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None
        if url:
            self.logger.info("GitHub PR created: %s", url)
        return url

    def _finalize_git_repo(self) -> None:
        if self.git_repo is None:
            return
        try:
            if self.git_snapshot_root is None:
                raise RuntimeError("Git snapshot root is not configured")
            try:
                managed_rel = self.git_snapshot_root.relative_to(self.git_repo)
            except ValueError as exc:
                raise RuntimeError("Git snapshot root is outside the active Git worktree") from exc
            managed_arg = managed_rel.as_posix() if managed_rel.parts else "."
            self._git_command(["add", "-A", "--", managed_arg])
            staged = self._git_command(["diff", "--cached", "--quiet"], check=False)
            if staged.returncode == 0:
                self.logger.info("Git snapshot: no changes to commit")
                return
            if staged.returncode != 1:
                raise RuntimeError(staged.stderr.strip() or "Unable to inspect staged Git changes")
            message_template = str(self.git_cfg.get("commit_message") or "ConfigBackup {hostname} {run_id}")
            message = self._format_git_template(message_template)
            self._git_command(["commit", "-m", message])
            commit = self._git_command(["rev-parse", "--short", "HEAD"]).stdout.strip()
            self.logger.info("Git snapshot committed as %s", commit)
            if self.git_cfg.get("push"):
                remote = str(self.git_cfg.get("remote_name") or "origin")
                branch = self.git_branch if self.git_mode == "pull_request" else str(self.git_cfg.get("branch") or "main")
                push_args = ["push"]
                if self.git_mode == "pull_request" and self.git_force_push:
                    push_args.append("--force-with-lease")
                push_args.extend([remote, f"HEAD:refs/heads/{branch}"])
                self._git_command(push_args)
                self.logger.info("Git snapshot pushed to %s/%s", remote, branch)
                if self.git_mode == "pull_request":
                    self.git_open_pr_url = self._ensure_github_pull_request()
        finally:
            if self.git_mode == "pull_request":
                self._cleanup_git_worktree()

    def _rollback_git_repo(self) -> None:
        if self.git_repo is None or self.dry_run:
            return
        if self.git_mode == "pull_request":
            # The automation worktree is isolated from the user's main checkout. Discard it wholesale.
            self._cleanup_git_worktree()
            return
        if not (self.git_repo / ".git").exists():
            return
        has_head = self._git_command(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0
        if has_head:
            self._git_command(["reset", "--hard", "HEAD"])
            self._git_command(["clean", "-fd"])
        else:
            # Dedicated repo was clean before the run; remove generated working-tree files only.
            for child in self.git_repo.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)

    def _close_logging(self) -> None:
        for handler in list(self.logger.handlers):
            try:
                handler.flush()
                handler.close()
            finally:
                self.logger.removeHandler(handler)

    def _run_task(self, raw_task: dict[str, Any]) -> TaskResult:
        task = self.resolve_task_runtime(raw_task)
        name = task["name"]
        required = bool(task.get("required", True))
        deps = task.get("depends_on") or []
        failed_deps = [d for d in deps if d not in self.results or self.results[d].status not in {"success", "dry-run"}]
        if failed_deps and not task.get("run_on_failure"):
            msg = f"Skipped because dependencies did not succeed: {', '.join(failed_deps)}"
            self.logger.warning("Task %s: %s", name, msg)
            return TaskResult(name, "skipped", msg, required)
        if self.dry_run and not task.get("run_on_failure"):
            task_defs = {t["name"]: t for t in self.cfg.get("tasks", [])}
            suppressed_collectors = [
                d for d in deps
                if self.results.get(d) and self.results[d].status == "dry-run"
                and task_defs.get(d, {}).get("type") == "execute"
            ]
            if suppressed_collectors:
                msg = "Skipped in dry-run because collector execution is suppressed: " + ", ".join(suppressed_collectors)
                self.logger.info("Task %s: %s", name, msg)
                return TaskResult(name, "skipped", msg, required)

        started_dt = now_local()
        started_perf = time.monotonic()
        result = TaskResult(name=name, status="success", required=required, started=started_dt.isoformat(timespec="seconds"))
        self.logger.info("Task %s (%s/%s) started", name, task["phase"], task["type"])
        try:
            if task["type"] == "execute":
                self._task_execute(task, result)
            elif task["type"] == "command":
                self._task_command(task, result)
            elif task["type"] in {"file", "directory", "glob"}:
                self._task_backup_sources(task, result)
            else:  # pragma: no cover
                raise ConfigError(f"Unsupported task type {task['type']}")
            if result.status == "failed":
                pass
            elif self.dry_run:
                result.status = "dry-run"
            else:
                result.status = "success"
        except Exception as exc:
            result.status = "failed"
            result.message = str(exc)
            self.logger.exception("Task %s failed: %s", name, exc)
        result.ended = now_local().isoformat(timespec="seconds")
        result.duration_seconds = time.monotonic() - started_perf
        self.logger.info("Task %s finished: %s (%.2fs)%s", name, result.status, result.duration_seconds, f" - {result.message}" if result.message else "")
        return result

    def _task_execute(self, task: dict[str, Any], result: TaskResult) -> None:
        output_dir = Path(task.get("output_directory") or self.variables_for_task(task)["CONFIGBACKUP_OUTPUT"])
        if task.get("clean_output"):
            if self.dry_run:
                self.logger.info("DRY-RUN: would clean output directory %s", output_dir)
            else:
                self._safe_clean_directory(output_dir, allow_outside_staging=bool(task.get("allow_clean_outside_staging", False)))
        if not self.dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        if self.dry_run:
            self.logger.info("DRY-RUN: would execute %s", self._format_command(task))
            result.message = "command execution suppressed in dry-run"
            return
        completed = self._run_subprocess(task, capture_stdout=True)
        result.return_code = completed.returncode
        if completed.stdout:
            text = completed.stdout.decode(errors="replace").rstrip()
            if text:
                self.logger.info("Task %s stdout:\n%s", task["name"], text)
        if completed.returncode != 0:
            raise RuntimeError(f"Command returned exit code {completed.returncode}")

    def _task_command(self, task: dict[str, Any], result: TaskResult) -> None:
        if self.dry_run:
            self.logger.info("DRY-RUN: would execute %s and archive stdout to %s", self._format_command(task), task["output"])
            result.message = "command execution suppressed in dry-run"
            return
        completed = self._run_subprocess(task, capture_stdout=True)
        result.return_code = completed.returncode
        if completed.returncode != 0:
            raise RuntimeError(f"Command returned exit code {completed.returncode}")
        with tempfile.NamedTemporaryFile(prefix="configbackup-command-", delete=False) as handle:
            handle.write(completed.stdout or b"")
            temp_path = Path(handle.name)
        try:
            logical = self._normalize_destination(Path(task["output"]))
            status = self._store_one_file(task, temp_path, logical, source_display=f"command:{task['name']}")
            result.counts[status] += 1
            if task.get("stderr") == "capture" and completed.stderr:
                stderr_output = task.get("stderr_output") or (str(task["output"]) + ".stderr")
                with tempfile.NamedTemporaryFile(prefix="configbackup-stderr-", delete=False) as err_handle:
                    err_handle.write(completed.stderr)
                    err_path = Path(err_handle.name)
                try:
                    err_logical = self._normalize_destination(Path(stderr_output))
                    err_status = self._store_one_file(task, err_path, err_logical, source_display=f"stderr:{task['name']}")
                    result.counts[err_status] += 1
                finally:
                    err_path.unlink(missing_ok=True)
        finally:
            temp_path.unlink(missing_ok=True)

    def _run_subprocess(self, task: dict[str, Any], capture_stdout: bool) -> subprocess.CompletedProcess[bytes]:
        executable = str(task["executable"])
        arguments = [str(a) for a in (task.get("arguments") or [])]
        cmd = [executable, *arguments]
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (task.get("environment") or {}).items()})
        env.update(self.variables_for_task(task))
        cwd = task.get("working_directory") or None
        stderr_mode = task.get("stderr", "log")
        stderr_target: Any
        if stderr_mode == "discard":
            stderr_target = subprocess.DEVNULL
        elif stderr_mode == "merge":
            stderr_target = subprocess.STDOUT
        else:
            stderr_target = subprocess.PIPE
        self.logger.info("Executing: %s", self._format_command(task))
        try:
            completed = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE if capture_stdout else None,
                stderr=stderr_target,
                timeout=task.get("timeout") or None,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Command timed out after {task.get('timeout')} seconds") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Executable not found: {executable}") from exc
        if stderr_mode == "log" and completed.stderr:
            text = completed.stderr.decode(errors="replace").rstrip()
            if text:
                level = logging.ERROR if completed.returncode else logging.WARNING
                self.logger.log(level, "Task %s stderr:\n%s", task["name"], text)
        return completed

    def _format_command(self, task: dict[str, Any]) -> str:
        parts = [str(task.get("executable", "")), *[str(a) for a in (task.get("arguments") or [])]]
        return " ".join(repr(p) if re.search(r"\s", p) else p for p in parts)

    def _safe_clean_directory(self, path: Path, *, allow_outside_staging: bool = False) -> None:
        path = path.absolute()
        resolved = path.resolve(strict=False)
        staging_resolved = self.staging_root.resolve(strict=False)
        if (
            resolved == Path(path.anchor).resolve(strict=False)
            or self._points_within_backup(path)
            or resolved in {self.archive_root.resolve(strict=False), self.internal_root.resolve(strict=False), staging_resolved}
        ):
            raise RuntimeError(f"Refusing to clean unsafe directory: {path}")
        if not allow_outside_staging:
            try:
                resolved.relative_to(staging_resolved)
            except ValueError as exc:
                raise RuntimeError(
                    f"clean_output is restricted to CONFIGBACKUP_STAGING by default: {path}. "
                    "Set allow_clean_outside_staging: true only if this external path is intentionally disposable."
                ) from exc
        if path.exists():
            if path.is_symlink():
                raise RuntimeError(f"Refusing to recursively clean a symlinked output directory: {path}")
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    def _task_backup_sources(self, task: dict[str, Any], result: TaskResult) -> None:
        self._validate_scan_roots(task)
        discovered = self._discover_sources(task)
        seen_keys: set[str] = set()
        for src, logical in discovered:
            self.stats.files_scanned += 1
            status = self._store_one_file(task, src, logical)
            result.counts[status] += 1
            seen_keys.add(posix_rel(logical))
        self._process_missing(task, seen_keys)
        task_state = self.state.task(task["name"])
        task_state["last_success"] = iso_now()

    def _validate_scan_roots(self, task: dict[str, Any]) -> None:
        for source_value in self._as_list(task.get("source")):
            source_str = str(source_value)
            if not has_glob_magic(source_str):
                candidate_source = Path(source_str)
                if candidate_source.exists() and self._points_within_backup(candidate_source):
                    raise RuntimeError(f"Refusing to back up a source inside (or linked into) the backup root: {candidate_source}")
            if has_glob_magic(source_str) or task["type"] == "glob":
                base = glob_static_base(source_str)
                if not base.exists():
                    raise RuntimeError(f"Glob base path does not exist for task {task['name']!r}: {base}")
                if not base.is_dir():
                    raise RuntimeError(f"Glob base path is not a directory: {base}")
                if self._points_within_backup(base):
                    raise RuntimeError(f"Refusing to use a glob base inside (or linked into) the backup root: {base}")
                self._probe_directory(base)
                continue
            source = Path(source_str)
            if task["type"] == "file":
                if source.exists():
                    if not source.is_file():
                        raise RuntimeError(f"Configured file source is not a file: {source}")
                else:
                    if not source.parent.exists():
                        raise RuntimeError(f"Parent directory for file source is unavailable: {source.parent}")
                    self._probe_directory(source.parent)
                continue
            if task["type"] == "directory":
                if not source.exists():
                    raise RuntimeError(f"Directory source does not exist: {source}")
                if not source.is_dir():
                    raise RuntimeError(f"Configured directory source is not a directory: {source}")
                self._probe_directory(source)

    def _probe_directory(self, path: Path) -> None:
        try:
            with os.scandir(path) as entries:
                next(entries, None)
        except OSError as exc:
            raise RuntimeError(f"Cannot read source directory {path}: {exc}") from exc

    def _discover_sources(self, task: dict[str, Any]) -> list[tuple[Path, Path]]:
        include = [str(x) for x in (task.get("include") or [])]
        exclude = [str(x) for x in (task.get("exclude") or [])]
        destination = Path(task["destination"]) if task.get("destination") else None
        results: dict[str, tuple[Path, Path]] = {}
        for source_value in self._as_list(task.get("source")):
            source_str = str(source_value)
            if not has_glob_magic(source_str):
                candidate_source = Path(source_str)
                if candidate_source.exists() and self._points_within_backup(candidate_source):
                    raise RuntimeError(f"Refusing to back up a source inside (or linked into) the backup root: {candidate_source}")
            if has_glob_magic(source_str) or task["type"] == "glob":
                base = glob_static_base(source_str)
                matches = [Path(p) for p in glob.glob(source_str, recursive=True)]
                for match in matches:
                    if self._points_within_backup(match):
                        continue
                    if match.is_dir():
                        for file_path in self._walk_files(match):
                            try:
                                rel = file_path.relative_to(base)
                            except ValueError:
                                rel = file_path.relative_to(match)
                            if self._filter_rel(rel, include, exclude):
                                logical = self._logical_for_source(file_path, base if self._is_within(file_path.absolute(), base.absolute()) else match, destination)
                                results[str(file_path.absolute())] = (file_path, logical)
                    elif match.is_file():
                        try:
                            rel = match.relative_to(base)
                        except ValueError:
                            rel = Path(match.name)
                        if self._filter_rel(rel, include, exclude):
                            if destination is not None:
                                logical = self._normalize_destination(destination / rel)
                            else:
                                logical = self._normalize_destination(source_to_logical(match))
                            results[str(match.absolute())] = (match, logical)
                continue

            source = Path(source_str)
            if task["type"] == "file" or source.is_file():
                if source.is_file():
                    if self._points_within_backup(source):
                        raise RuntimeError(f"Refusing to back up a file from inside the backup root: {source}")
                    if self._filter_rel(Path(source.name), include, exclude):
                        results[str(source.absolute())] = (source, self._logical_for_single(source, destination))
                continue
            if source.is_dir():
                for file_path in self._walk_files(source):
                    rel = file_path.relative_to(source)
                    if self._filter_rel(rel, include, exclude):
                        logical = self._logical_for_source(file_path, source, destination)
                        results[str(file_path.absolute())] = (file_path, logical)
        return sorted(results.values(), key=lambda x: str(x[0]))

    def _points_within_backup(self, path: Path) -> bool:
        """Return True for archive/Git destinations that must never be re-ingested."""
        protected_roots = [self.root]
        if self.git_control_repo is not None:
            protected_roots.append(self.git_control_repo)
        if self.git_repo is not None and self.git_repo != self.git_control_repo:
            protected_roots.append(self.git_repo)
        for protected in protected_roots:
            if self._is_within(path.absolute(), protected):
                return True
            try:
                if self._is_within(path.resolve(strict=False), protected.resolve(strict=False)):
                    return True
            except OSError:
                continue
        return False

    def _walk_files(self, root: Path) -> Iterator[Path]:
        visited_dirs: set[str] = set()
        for current, dirs, files in os.walk(root, followlinks=True, onerror=lambda exc: (_ for _ in ()).throw(exc)):
            current_path = Path(current).absolute()
            try:
                current_real = str(current_path.resolve())
            except OSError:
                current_real = str(current_path)
            if current_real in visited_dirs:
                dirs[:] = []
                continue
            visited_dirs.add(current_real)

            filtered_dirs: list[str] = []
            for d in dirs:
                candidate = (current_path / d).absolute()
                if self._points_within_backup(candidate):
                    continue
                try:
                    real = str(candidate.resolve())
                except OSError:
                    real = str(candidate)
                if real in visited_dirs:
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for filename in files:
                path = Path(current) / filename
                if self._points_within_backup(path):
                    continue
                if path.is_file():
                    yield path

    def _filter_rel(self, rel: Path, include: list[str], exclude: list[str]) -> bool:
        rels = rel.as_posix()
        if include and not path_matches(rels, include):
            return False
        if exclude and path_matches(rels, exclude):
            return False
        return True

    def _logical_for_single(self, src: Path, destination: Path | None) -> Path:
        if destination is not None:
            if destination.suffix or not str(destination).endswith(("/", "\\")):
                # If destination looks file-like, use it; otherwise caller can explicitly include filename.
                if destination.name and destination.name != "." and (destination.suffix or destination.name == src.name):
                    return self._normalize_destination(destination)
            return self._normalize_destination(destination / src.name)
        return self._normalize_destination(source_to_logical(src))

    def _logical_for_source(self, file_path: Path, source_root: Path, destination: Path | None) -> Path:
        rel = file_path.relative_to(source_root)
        if destination is not None:
            return self._normalize_destination(destination / rel)
        return self._normalize_destination(source_to_logical(source_root) / rel)

    def _normalize_destination(self, path: Path) -> Path:
        raw = str(path).replace("\\", "/")
        raw = re.sub(r"^[A-Za-z]:", lambda m: m.group(0).rstrip(":"), raw)
        raw = raw.lstrip("/")
        normalized = Path(raw)
        if any(part == ".." for part in normalized.parts):
            raise ConfigError(f"Destination may not contain '..': {path}")
        return normalized

    def _backup_one_file(
        self, task: dict[str, Any], src: Path, logical: Path, source_display: str | None = None, *, count_stats: bool = True
    ) -> str:
        task_state = self.state.task(task["name"])
        key = posix_rel(logical)
        source_identity = source_display or str(src.absolute())
        owner = self._logical_owners.get(key)
        if owner is not None and owner != (task["name"], source_identity):
            raise RuntimeError(
                f"Archive logical-path collision for {key!r}: "
                f"{owner[0]} ({owner[1]}) and {task['name']} ({source_identity})"
            )
        self._logical_owners[key] = (task["name"], source_identity)
        file_state = task_state["files"].setdefault(
            key,
            {"missing_runs": 0, "versions": [], "deleted_generations": [], "active": True},
        )
        file_state.setdefault("versions", [])
        file_state.setdefault("deleted_generations", [])
        file_state["active"] = True
        file_state["missing_runs"] = 0
        file_state["last_seen"] = iso_now()
        src_hash = hash_file(src, self.hash_algorithm)
        size = src.stat().st_size
        latest = self._latest_version(file_state["versions"])
        if latest and latest.get("hash") == src_hash:
            if count_stats:
                self.stats.unchanged += 1
            self.logger.debug("UNCHANGED %s", source_display or src)
            return "unchanged"
        is_new = not file_state["versions"]
        if count_stats:
            if is_new:
                self.stats.new += 1
            else:
                self.stats.changed += 1
        destination_dir = self.archive_root / logical.parent
        existing_names = {Path(v["path"]).name for v in file_state["versions"]}
        if destination_dir.exists():
            existing_names.update(p.name for p in destination_dir.iterdir() if p.is_file())
        versioned_name = make_versioned_filename(logical.name, now_local(), existing_names)
        destination = destination_dir / versioned_name
        rel_destination = destination.relative_to(self.root).as_posix()
        if destination in self._written_paths:
            raise RuntimeError(f"Destination collision detected: {destination}")
        self._written_paths.add(destination)
        if self.dry_run:
            self.logger.info("DRY-RUN: would store %s -> %s", source_display or src, destination)
        else:
            atomic_copy(src, destination)
            stored_hash = hash_file(destination, self.hash_algorithm)
            if stored_hash != src_hash:
                destination.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Source changed while being copied or copy verification failed: {source_display or src}"
                )
            self.logger.info("STORED %s -> %s", source_display or src, destination)
        file_state["versions"].append(
            {
                "path": rel_destination,
                "hash": src_hash,
                "size": size,
                "created": iso_now(),
                "source": source_identity,
            }
        )
        if count_stats:
            self.stats.stored += 1
            self.stats.bytes_written += size
        return "new" if is_new else "changed"

    def _latest_version(self, versions: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not versions:
            return None
        return max(versions, key=lambda v: parse_iso(v["created"]))

    def _process_missing(self, task: dict[str, Any], seen_keys: set[str]) -> None:
        deletion_cfg = deep_merge(self.cfg.get("deletion") or {}, task.get("deletion") or {})
        if not deletion_cfg.get("enabled", True):
            return
        task_state = self.state.task(task["name"])
        active_keys = [
            k for k, s in task_state["files"].items()
            if s.get("active", True) and (s.get("versions") or s.get("git_hash"))
        ]
        missing_keys = [k for k in active_keys if k not in seen_keys]
        if not missing_keys:
            return
        self.stats.missing += len(missing_keys)
        percent = (len(missing_keys) / len(active_keys) * 100.0) if active_keys else 0.0
        max_percent = deletion_cfg.get("max_percent_per_run", 20)
        max_items = deletion_cfg.get("max_items_per_run", 500)
        percent_guard = (
            max_percent is not None
            and len(missing_keys) >= int(deletion_cfg.get("min_items_for_percent", 10))
            and percent > float(max_percent)
        )
        item_guard = max_items is not None and len(missing_keys) > int(max_items)
        if item_guard or percent_guard:
            self.logger.error(
                "Deletion guard triggered for task %s: %d/%d active files missing (%.1f%%). Missing/deletion state not advanced.",
                task["name"], len(missing_keys), len(active_keys), percent,
            )
            return
        threshold = int(deletion_cfg.get("missing_runs", 2))
        for key in missing_keys:
            file_state = task_state["files"][key]
            file_state["missing_runs"] = int(file_state.get("missing_runs", 0)) + 1
            self.logger.warning("MISSING %s (%d/%d)", key, file_state["missing_runs"], threshold)
            if file_state["missing_runs"] >= threshold:
                self._mark_deleted(task, key, file_state)

    def _archive_state_path(self, relative_path: str) -> Path:
        rel = Path(relative_path)
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise RuntimeError(f"Unsafe path found in ConfigBackup state: {relative_path!r}")
        candidate = self.root / rel
        if not self._is_within(candidate.absolute(), self.root):
            raise RuntimeError(f"State path escapes backup root: {relative_path!r}")
        return candidate

    def _mark_deleted(self, task: dict[str, Any], key: str, file_state: dict[str, Any]) -> None:
        deletion_time = now_local()
        storage = str(task.get("storage", "filesystem")).lower()
        versions = list(file_state.get("versions") or [])
        has_git = bool(file_state.get("git_hash") or file_state.get("git_path"))
        if not versions and not has_git:
            return

        deleted_base: Path | None = None
        generation: dict[str, Any] | None = None
        if storage in {"filesystem", "both"} and versions:
            date_dir = deletion_time.strftime("%Y%m%d")
            logical = Path(key)
            deleted_base = self.deleted_root / date_dir / sanitize_component(task["name"]) / logical.parent
            moved_versions: list[dict[str, Any]] = []
            for version in versions:
                src = self._archive_state_path(version["path"])
                dst = deleted_base / src.name
                if not self.dry_run:
                    ensure_parent(dst)
                    if src.exists():
                        if dst.exists():
                            dst = dst.with_name(f"{dst.stem}-{self.run_id}{dst.suffix}")
                        shutil.move(str(src), str(dst))
                        self._remove_empty_parents(src.parent)
                new_version = copy.deepcopy(version)
                new_version["path"] = dst.relative_to(self.root).as_posix()
                moved_versions.append(new_version)
            metadata_path = deleted_base / f"{logical.name}.deletion.{self.run_id}.json"
            metadata = {
                "original_path": key,
                "task": task["name"],
                "deleted_detected": deletion_time.isoformat(timespec="seconds"),
                "versions": len(moved_versions),
            }
            if not self.dry_run:
                atomic_json_write(metadata_path, metadata)
            generation = {
                "deleted_at": deletion_time.isoformat(timespec="seconds"),
                "original_path": key,
                "versions": moved_versions,
                "metadata_path": metadata_path.relative_to(self.root).as_posix(),
            }
            file_state.setdefault("deleted_generations", []).append(generation)
            file_state["versions"] = []

        if storage in {"git", "both"} and has_git:
            git_path = file_state.get("git_path")
            if git_path and self.git_repo is not None:
                destination = self.git_repo / Path(str(git_path))
                if self.dry_run:
                    self.logger.info("DRY-RUN: would remove deleted Git snapshot %s", destination)
                elif destination.exists():
                    destination.unlink()
                    self._remove_empty_git_parents(destination.parent)
            file_state["git_hash"] = None
            file_state["git_size"] = None
            file_state["git_deleted_at"] = deletion_time.isoformat(timespec="seconds")

        file_state["active"] = False
        file_state["missing_runs"] = 0
        file_state["deleted_at"] = deletion_time.isoformat(timespec="seconds")
        self.stats.deleted += 1
        if deleted_base is not None:
            self.logger.warning("DELETED %s -> %s", key, deleted_base)
        else:
            self.logger.warning("DELETED %s from Git snapshot", key)

    def _remove_empty_git_parents(self, path: Path) -> None:
        if self.git_repo is None:
            return
        repo = self.git_repo.resolve(strict=False)
        current = path
        for _ in range(30):
            if current.resolve(strict=False) == repo:
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _run_retention_all(self, eligible_names: set[str] | None = None) -> None:
        tasks_by_name = {t["name"]: t for t in self.cfg["tasks"]}
        for name, task_state in self.state.data.get("tasks", {}).items():
            if eligible_names is not None and name not in eligible_names:
                continue
            task = tasks_by_name.get(name)
            if not task:
                continue
            resolved = self.resolve_task_runtime(task)
            self._apply_task_retention(resolved, task_state)

    def _apply_task_retention(self, task: dict[str, Any], task_state: dict[str, Any]) -> None:
        retention = task.get("retention") or normalize_retention({})
        active_policy = retention["active"]
        deleted_policy = retention["deleted"]

        active_groups: dict[str, list[dict[str, Any]]] = {}
        for key, state in task_state.get("files", {}).items():
            versions = state.get("versions") or []
            if versions:
                active_groups[key] = versions
        active_prune = self._retention_candidates(active_groups, active_policy)
        active_prune = self._apply_task_size_limit(active_groups, active_policy, active_prune)
        self._prune_versions(active_prune, task_state, deleted=False)

        deleted_groups: dict[str, list[dict[str, Any]]] = {}
        deleted_candidates: list[dict[str, Any]] = []
        eligible_generations: list[dict[str, Any]] = []
        for key, state in list(task_state.get("files", {}).items()):
            for generation in list(state.get("deleted_generations") or []):
                deleted_at = parse_iso(generation["deleted_at"])
                age_days = max(0, (now_local() - deleted_at).days)
                purge_after = deleted_policy.get("purge_after_days")
                if purge_after is not None and age_days >= int(purge_after):
                    self._purge_deleted_generation(state, generation)
                    continue
                if age_days < int(deleted_policy.get("grace_days", 30)):
                    continue
                group_key = f"{key}::{generation['deleted_at']}"
                versions = generation.get("versions") or []
                deleted_groups[group_key] = versions
                deleted_candidates.extend(
                    self._retention_candidates({group_key: versions}, deleted_policy, deleted_age_days=age_days)
                )
                eligible_generations.append(generation)

        if deleted_groups:
            deleted_candidates = self._apply_task_size_limit(deleted_groups, deleted_policy, deleted_candidates)
            for generation in eligible_generations:
                self._prune_generation_versions(generation, deleted_candidates)

    def _retention_candidates(
        self,
        groups: dict[str, list[dict[str, Any]]],
        policy: dict[str, Any],
        deleted_age_days: int | None = None,
    ) -> list[dict[str, Any]]:
        mode = policy.get("mode", "indefinite")
        if mode == "indefinite":
            return []
        now = now_local()
        candidates: list[dict[str, Any]] = []
        min_versions = int(policy.get("min_versions", 3))
        for _, versions in groups.items():
            ordered = sorted(versions, key=lambda v: parse_iso(v["created"]), reverse=True)
            protected_ids = {id(v) for v in ordered[:min_versions]}
            keep_ids = set(protected_ids)
            if mode == "simple":
                max_versions = policy.get("max_versions")
                max_age_days = policy.get("max_age_days")
                for idx, version in enumerate(ordered):
                    if id(version) in protected_ids:
                        continue
                    keep = True
                    if max_versions is not None and idx >= int(max_versions):
                        keep = False
                    if max_age_days is not None:
                        age = deleted_age_days if deleted_age_days is not None else max(0, (now - parse_iso(version["created"])).days)
                        if age > int(max_age_days):
                            keep = False
                    if keep:
                        keep_ids.add(id(version))
            elif mode == "tiered":
                buckets: dict[tuple[int, Any], dict[str, Any]] = {}
                cumulative = 0
                tiers = policy["tiers"]
                for version in ordered:
                    if id(version) in protected_ids:
                        continue
                    age = max(0, (now - parse_iso(version["created"])).days)
                    selected_tier: tuple[int, dict[str, Any]] | None = None
                    lower = 0
                    for idx, tier in enumerate(tiers):
                        if tier.get("forever"):
                            selected_tier = (idx, tier)
                            break
                        upper = lower + int(tier["duration_days"])
                        if lower <= age < upper:
                            selected_tier = (idx, tier)
                            break
                        lower = upper
                    if selected_tier is None:
                        continue
                    idx, tier = selected_tier
                    interval = tier["interval"]
                    if interval == "all":
                        keep_ids.add(id(version))
                    else:
                        created = parse_iso(version["created"])
                        bucket = self._bucket_key(created, interval)
                        key = (idx, bucket)
                        existing = buckets.get(key)
                        if existing is None or parse_iso(version["created"]) > parse_iso(existing["created"]):
                            buckets[key] = version
                keep_ids.update(id(v) for v in buckets.values())
                # Optional hard ceilings may be combined with tiered retention.
                max_versions = policy.get("max_versions")
                max_age_days = policy.get("max_age_days")
                for idx, version in enumerate(ordered):
                    if id(version) in protected_ids:
                        continue
                    if max_versions is not None and idx >= int(max_versions):
                        keep_ids.discard(id(version))
                    if max_age_days is not None:
                        age = deleted_age_days if deleted_age_days is not None else max(0, (now - parse_iso(version["created"])).days)
                        if age > int(max_age_days):
                            keep_ids.discard(id(version))
            for version in ordered:
                if id(version) not in keep_ids:
                    candidates.append(version)
        return candidates

    def _bucket_key(self, timestamp: dt.datetime, interval: str) -> Any:
        local = timestamp.astimezone()
        if interval == "daily":
            return (local.year, local.month, local.day)
        if interval == "weekly":
            iso = local.isocalendar()
            return (iso.year, iso.week)
        if interval == "monthly":
            return (local.year, local.month)
        if interval == "yearly":
            return local.year
        return timestamp.isoformat()

    def _apply_task_size_limit(
        self,
        groups: dict[str, list[dict[str, Any]]],
        policy: dict[str, Any],
        existing_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        max_size = policy.get("max_size_bytes")
        if max_size is None:
            return existing_candidates
        all_versions = [v for versions in groups.values() for v in versions]
        candidate_ids = {id(v) for v in existing_candidates}
        kept = [v for v in all_versions if id(v) not in candidate_ids]
        total = sum(int(v.get("size", 0)) for v in kept)
        if total <= max_size:
            return existing_candidates
        min_versions = int(policy.get("min_versions", 3))
        protected_ids: set[int] = set()
        for versions in groups.values():
            ordered = sorted(versions, key=lambda v: parse_iso(v["created"]), reverse=True)
            protected_ids.update(id(v) for v in ordered[:min_versions])
        optional_kept = sorted(
            [v for v in kept if id(v) not in protected_ids],
            key=lambda v: parse_iso(v["created"]),
        )
        result = list(existing_candidates)
        for version in optional_kept:
            if total <= max_size:
                break
            result.append(version)
            total -= int(version.get("size", 0))
        if total > max_size:
            self.logger.warning(
                "Retention size target %s cannot be reached without violating min_versions=%d; protected data is %s",
                human_size(max_size), min_versions, human_size(total),
            )
        return result

    def _prune_versions(self, candidates: list[dict[str, Any]], task_state: dict[str, Any], deleted: bool) -> None:
        candidate_paths = {v["path"] for v in candidates}
        if not candidate_paths:
            return
        for state in task_state.get("files", {}).values():
            versions = state.get("versions") or []
            kept = []
            for version in versions:
                if version["path"] in candidate_paths:
                    self._delete_version_file(version)
                else:
                    kept.append(version)
            state["versions"] = kept

    def _prune_generation_versions(self, generation: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
        candidate_paths = {v["path"] for v in candidates}
        if not candidate_paths:
            return
        kept = []
        for version in generation.get("versions") or []:
            if version["path"] in candidate_paths:
                self._delete_version_file(version)
            else:
                kept.append(version)
        generation["versions"] = kept

    def _delete_version_file(self, version: dict[str, Any]) -> None:
        path = self._archive_state_path(version["path"])
        size = int(version.get("size", 0))
        if self.dry_run:
            self.logger.info("DRY-RUN: retention would delete %s", path)
        else:
            path.unlink(missing_ok=True)
            self._remove_empty_parents(path.parent)
            self.logger.info("PRUNED %s", path)
        self.stats.pruned += 1
        self.stats.bytes_pruned += size

    def _purge_deleted_generation(self, state: dict[str, Any], generation: dict[str, Any]) -> None:
        for version in generation.get("versions") or []:
            self._delete_version_file(version)
        metadata = self._archive_state_path(generation.get("metadata_path", "")) if generation.get("metadata_path") else self.root
        if generation.get("metadata_path"):
            if self.dry_run:
                self.logger.info("DRY-RUN: would remove deletion metadata %s", metadata)
            else:
                metadata.unlink(missing_ok=True)
                self._remove_empty_parents(metadata.parent)
        state["deleted_generations"].remove(generation)

    def _remove_empty_parents(self, path: Path) -> None:
        for _ in range(20):
            if path in {self.root, self.archive_root, self.deleted_root, self.internal_root}:
                break
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent

    def _is_within(self, child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def _as_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def _write_run_manifest(self) -> None:
        if self.dry_run:
            return
        manifest = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "run_id": self.run_id,
            "started": self.start_time.isoformat(timespec="seconds"),
            "ended": iso_now(),
            "hostname": self.hostname,
            "stats": vars(self.stats),
            "tasks": {
                name: {
                    "status": result.status,
                    "message": result.message,
                    "required": result.required,
                    "counts": dict(result.counts),
                    "started": result.started,
                    "ended": result.ended,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "return_code": result.return_code,
                }
                for name, result in self.results.items()
            },
        }
        path = self.internal_root / "runs" / f"{self.run_id}.json"
        atomic_json_write(path, manifest)

    def _print_summary(self) -> None:
        success = sum(1 for r in self.results.values() if r.status in {"success", "dry-run"})
        failed = sum(1 for r in self.results.values() if r.status == "failed")
        skipped = sum(1 for r in self.results.values() if r.status == "skipped")
        elapsed = time.monotonic() - getattr(self, "_start_perf", time.monotonic())
        lines = [
            "",
            f"{APP_NAME} {APP_VERSION} summary",
            f"Tasks:          {len(self.results)}",
            f"Successful:     {success}",
            f"Failed:         {failed}",
            f"Skipped:        {skipped}",
            "",
            f"Files scanned:  {self.stats.files_scanned}",
            f"New:            {self.stats.new}",
            f"Changed:        {self.stats.changed}",
            f"Unchanged:      {self.stats.unchanged}",
            f"Missing:        {self.stats.missing}",
            f"Deleted:        {self.stats.deleted}",
            f"Stored:         {self.stats.stored}",
            f"Pruned:         {self.stats.pruned}",
            f"Data written:   {human_size(self.stats.bytes_written)}",
            f"Data pruned:    {human_size(self.stats.bytes_pruned)}",
            f"Elapsed:        {(now_local() - self.start_time).total_seconds():.1f} seconds",
        ]
        print("\n".join(lines))


def redact_for_display(value: Any) -> Any:
    secret_re = re.compile(r"password|passwd|secret|token|api[_-]?key", re.I)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = "***REDACTED***" if secret_re.search(str(k)) else redact_for_display(v)
        return out
    if isinstance(value, list):
        return [redact_for_display(v) for v in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Versioned configuration backup utility")
    parser.add_argument("--config", "-c", default="configbackup.yaml", help="YAML configuration file")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files or executing commands")
    parser.add_argument("--validate", action="store_true", help="Validate configuration and exit")
    parser.add_argument("--show-config", action="store_true", help="Print resolved configuration and exit")
    parser.add_argument("--prune", action="store_true", help="Run retention processing only")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = ConfigLoader(Path(args.config)).load()
        if args.validate:
            print(f"Configuration valid: {args.config}")
            return 0
        if args.show_config:
            if yaml is None:
                raise ConfigError("PyYAML is required")
            print(yaml.safe_dump(redact_for_display(cfg), sort_keys=False))
            return 0
        engine = BackupEngine(cfg, dry_run=args.dry_run, prune_only=args.prune)
        engine._start_perf = time.monotonic()
        lock_path = engine.internal_root / "configbackup.lock"
        if args.dry_run:
            return engine.run()
        with single_instance_lock(lock_path):
            return engine.run()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except LockError as exc:
        print(f"LOCK ERROR: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
