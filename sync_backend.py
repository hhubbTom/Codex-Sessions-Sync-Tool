from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def default_codex_home() -> Path:
    return Path.home() / ".codex"


@dataclass
class Paths:
    codex_home: Path
    config_path: Path
    db_path: Path
    modern_db_path: Path
    session_index_path: Path
    sessions_root: Path
    backup_dir: Path


def resolve_paths(codex_home: str | None) -> Paths:
    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    return Paths(
        codex_home=home,
        config_path=home / "config.toml",
        db_path=home / "state_5.sqlite",
        modern_db_path=home / "sqlite" / "codex-dev.db",
        session_index_path=home / "session_index.jsonl",
        sessions_root=home / "sessions",
        backup_dir=home / "history_sync_backups",
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_provider_name(provider: str) -> str:
    value = provider.strip()
    if not value:
        raise RuntimeError("Provider cannot be empty.")
    return value


def classify_provider_kind(provider: str) -> str:
    return "official" if normalize_provider_name(provider) == "openai" else "third_party"


def parse_current_provider(config_text: str) -> str | None:
    match = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"', config_text)
    if not match:
        return None
    return normalize_provider_name(match.group(1))


def parse_current_model(config_text: str) -> str | None:
    match = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', config_text)
    return match.group(1) if match else None


def read_latest_thread_provider(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT model_provider
        FROM threads
        WHERE model_provider IS NOT NULL
          AND model_provider <> ''
        ORDER BY updated_at DESC, updated_at_ms DESC
        LIMIT 1
        """
    ).fetchone()
    if not row or not row[0]:
        return None
    return normalize_provider_name(row[0])


def read_provider_from_auth(paths: Paths) -> str | None:
    auth_path = paths.codex_home / "auth.json"
    if not auth_path.exists():
        return None
    try:
        raw = read_text(auth_path).strip()
    except OSError:
        return None
    if not raw or raw == "{}":
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload:
        return "openai"
    return None


def resolve_current_provider(
    paths: Paths,
    config_text: str,
    conn: sqlite3.Connection | None,
    mode: str,
) -> tuple[str, str]:
    config_provider = parse_current_provider(config_text)
    if config_provider:
        return config_provider, "config:model_provider"

    auth_provider = read_provider_from_auth(paths)
    if auth_provider:
        return auth_provider, "auth.json"

    if mode == "legacy_db" and conn is not None:
        latest_thread_provider = read_latest_thread_provider(conn)
        if latest_thread_provider:
            return latest_thread_provider, "threads:latest_updated"

    if mode == "session_files":
        latest_session_provider = read_provider_from_session_index(paths)
        if latest_session_provider:
            return latest_session_provider, "session_index:latest_updated"

    raise RuntimeError(
        "Could not determine the active provider. Set model_provider in config.toml or use sync --target-provider."
    )


def connect_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def has_legacy_storage(paths: Paths) -> bool:
    return paths.db_path.exists()


def has_modern_storage(paths: Paths) -> bool:
    return paths.session_index_path.exists() and paths.sessions_root.exists()


def storage_mode(paths: Paths) -> str:
    if has_legacy_storage(paths):
        return "legacy_db"
    if has_modern_storage(paths):
        return "session_files"
    return "missing"


def ensure_environment(paths: Paths, require_legacy_db: bool = False) -> str:
    if not paths.config_path.exists():
        raise RuntimeError(f"Missing config file: {paths.config_path}")
    mode = storage_mode(paths)
    if require_legacy_db and mode != "legacy_db":
        raise RuntimeError(f"Missing database file: {paths.db_path}")
    if mode == "missing":
        raise RuntimeError(
            "Missing Codex history storage. Expected state_5.sqlite or session_index.jsonl + sessions/."
        )
    return mode


def query_provider_counts(conn: sqlite3.Connection) -> OrderedDict[str, int]:
    counts = OrderedDict()
    for provider, count in conn.execute(
        """
        SELECT model_provider, COUNT(*)
        FROM threads
        GROUP BY model_provider
        ORDER BY COUNT(*) DESC, model_provider ASC
        """
    ):
        counts[provider or "(empty)"] = count
    return counts


def current_provider_for_movement(
    paths: Paths,
    config_text: str,
    mode: str,
    conn: sqlite3.Connection | None,
) -> tuple[str, str]:
    config_provider = parse_current_provider(config_text)
    if config_provider:
        return config_provider, "config:model_provider"
    auth_provider = read_provider_from_auth(paths)
    if auth_provider:
        return auth_provider, "auth.json"
    return resolve_current_provider(paths, config_text, conn, mode)


def query_provider_counts_from_session_index(paths: Paths) -> OrderedDict[str, int]:
    counts = OrderedDict()
    for row in iter_session_index_rows(paths):
        session_path = session_path_for_thread_id(paths, row["id"])
        provider, status = read_session_meta_provider(session_path)
        key = provider if status == "ok" and provider else "(empty)"
        counts[key] = counts.get(key, 0) + 1
    return OrderedDict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def iter_rollout_paths(conn: sqlite3.Connection) -> list[Path]:
    paths: list[Path] = []
    for (rollout_path,) in conn.execute(
        """
        SELECT DISTINCT rollout_path
        FROM threads
        WHERE rollout_path IS NOT NULL
          AND rollout_path <> ''
        """
    ):
        paths.append(Path(rollout_path))
    return paths


def iter_session_index_rows(paths: Paths) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not paths.session_index_path.exists():
        return rows
    try:
        with paths.session_index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                thread_id = payload.get("id")
                if not isinstance(thread_id, str) or not thread_id.strip():
                    continue
                rows.append(payload)
    except OSError:
        return []
    return rows


def session_path_for_thread_id(paths: Paths, thread_id: str) -> Path:
    if paths.sessions_root.exists():
        for candidate in paths.sessions_root.rglob("*.jsonl"):
            session_meta_obj, status = read_session_meta(candidate)
            if status != "ok" or not session_meta_obj:
                continue
            payload = session_meta_obj.get("payload")
            if isinstance(payload, dict) and payload.get("id") == thread_id:
                return candidate
    return paths.sessions_root / f"{thread_id}.jsonl"


def iso_to_epoch_seconds(value: str | None) -> int:
    if not value or not isinstance(value, str):
        return 0
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


def read_provider_from_session_index(paths: Paths) -> str | None:
    rows = iter_session_index_rows(paths)
    if not rows:
        return None
    latest = max(rows, key=lambda item: iso_to_epoch_seconds(str(item.get("updated_at") or "")))
    provider, status = read_session_meta_provider(session_path_for_thread_id(paths, latest["id"]))
    if status == "ok" and provider:
        return normalize_provider_name(provider)
    return None


def read_session_meta_provider(path: Path) -> tuple[str | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None, "read_error"

    if not first_line:
        return None, "empty"

    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return None, "invalid_json"

    if not isinstance(payload, dict) or payload.get("type") != "session_meta":
        return None, "no_session_meta"

    meta = payload.get("payload")
    if not isinstance(meta, dict):
        return None, "bad_payload"

    provider = meta.get("model_provider")
    if not isinstance(provider, str):
        return None, "no_provider"
    return provider, "ok"


def read_session_meta(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None, "read_error"

    if not first_line:
        return None, "empty"

    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return None, "invalid_json"

    if not isinstance(payload, dict) or payload.get("type") != "session_meta":
        return None, "no_session_meta"

    meta = payload.get("payload")
    if not isinstance(meta, dict):
        return None, "bad_payload"
    return payload, "ok"


def scan_session_files(current_provider: str, rollout_paths: list[Path]) -> dict[str, int]:
    result: dict[str, int] = {
        "total_files": len(rollout_paths),
        "mismatched_provider": 0,
        "missing": 0,
        "read_error": 0,
        "empty": 0,
        "invalid_json": 0,
        "no_session_meta": 0,
        "bad_payload": 0,
        "no_provider": 0,
    }
    for rollout_path in rollout_paths:
        provider, status = read_session_meta_provider(rollout_path)
        if status == "ok":
            if provider != current_provider:
                result["mismatched_provider"] += 1
            continue
        if status in result:
            result[status] += 1
    return result


def list_backups(paths: Paths, limit: int = 20) -> list[dict[str, str]]:
    if not paths.backup_dir.exists():
        return []
    files = sorted(
        paths.backup_dir.glob("state_5.sqlite.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    output = []
    for item in files[:limit]:
        output.append(
            {
                "name": item.name,
                "path": str(item),
                "modified_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return output


def is_windows_platform() -> bool:
    return os.name == "nt"


def is_windows_style_path(value: str | None) -> bool:
    return isinstance(value, str) and (
        value.startswith("\\\\?\\") or bool(re.match(r"^[A-Za-z]:\\", value))
    )


def is_posix_style_path(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith("/")


def normalize_local_path(path: Path) -> str:
    resolved = path.resolve()
    if is_windows_platform():
        return "\\\\?\\" + str(resolved)
    return str(resolved)


def normalize_explicit_cwd(value: str) -> str:
    if is_windows_platform():
        if is_windows_style_path(value):
            stripped = value.removeprefix("\\\\?\\")
            return normalize_local_path(Path(stripped))
        return value
    if is_posix_style_path(value):
        return normalize_local_path(Path(value))
    return value


def is_cross_device_thread(cwd: str, rollout_path: str) -> bool:
    if is_windows_platform():
        return is_posix_style_path(cwd) or not is_windows_style_path(rollout_path)
    return is_windows_style_path(cwd) or is_windows_style_path(rollout_path)


def resolve_repair_target_cwd(conn: sqlite3.Connection, explicit_cwd: str | None) -> str:
    if explicit_cwd:
        return normalize_explicit_cwd(explicit_cwd)

    for (cwd,) in conn.execute(
        """
        SELECT cwd
        FROM threads
        WHERE cwd IS NOT NULL
          AND cwd <> ''
        ORDER BY updated_at DESC, updated_at_ms DESC
        """
    ):
        if is_windows_platform() and is_windows_style_path(cwd):
            return normalize_explicit_cwd(cwd)
        if not is_windows_platform() and is_posix_style_path(cwd):
            return normalize_explicit_cwd(cwd)

    return normalize_local_path(Path.cwd())


def resolve_local_session_path(paths: Paths, rollout_path: str) -> Path:
    stripped = rollout_path.removeprefix("\\\\?\\")
    candidate = Path(stripped)
    if candidate.exists():
        return candidate

    basename = Path(stripped.replace("\\", "/")).name
    sessions_root = paths.codex_home / "sessions"
    if basename and sessions_root.exists():
        matches = list(sessions_root.rglob(basename))
        if matches:
            return matches[0]
    return candidate


def get_status(paths: Paths) -> dict[str, object]:
    mode = ensure_environment(paths)
    config_text = read_text(paths.config_path)
    current_model = parse_current_model(config_text)

    if mode == "legacy_db":
        with connect_db(paths.db_path, readonly=True) as conn:
            current_provider, current_provider_source = resolve_current_provider(paths, config_text, conn, mode)
            movement_provider, _movement_provider_source = current_provider_for_movement(paths, config_text, mode, conn)
            counts = query_provider_counts(conn)
            total_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            moved_if_sync = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider <> ?",
                (movement_provider,),
            ).fetchone()[0]
            repair_candidates = 0
            for cwd, rollout_path in conn.execute("SELECT cwd, rollout_path FROM threads"):
                if is_cross_device_thread(cwd, rollout_path):
                    repair_candidates += 1
            rollout_paths = iter_rollout_paths(conn)
            session_scan = scan_session_files(current_provider, rollout_paths)
        db_path_text = str(paths.db_path)
    else:
        current_provider, current_provider_source = resolve_current_provider(paths, config_text, None, mode)
        movement_provider, _movement_provider_source = current_provider_for_movement(paths, config_text, mode, None)
        counts = query_provider_counts_from_session_index(paths)
        rows = iter_session_index_rows(paths)
        total_threads = len(rows)
        moved_if_sync = 0
        rollout_paths: list[Path] = []
        for row in rows:
            session_path = session_path_for_thread_id(paths, row["id"])
            rollout_paths.append(session_path)
            provider, status = read_session_meta_provider(session_path)
            if status == "ok" and provider != movement_provider:
                moved_if_sync += 1
        repair_candidates = 0
        session_scan = scan_session_files(current_provider, rollout_paths)
        db_path_text = str(paths.modern_db_path if paths.modern_db_path.exists() else paths.session_index_path)

    return {
        "codex_home": str(paths.codex_home),
        "config_path": str(paths.config_path),
        "db_path": db_path_text,
        "backup_dir": str(paths.backup_dir),
        "storage_mode": mode,
        "current_provider": current_provider,
        "current_provider_source": current_provider_source,
        "current_provider_kind": classify_provider_kind(current_provider),
        "current_model": current_model,
        "total_threads": total_threads,
        "movable_threads": moved_if_sync,
        "repair_candidates": repair_candidates,
        "movable_sessions": session_scan["mismatched_provider"],
        "session_scan": session_scan,
        "provider_counts": [{"provider": key, "count": value} for key, value in counts.items()],
        "backups": list_backups(paths),
    }


def make_backup(paths: Paths, label: str, timestamp: str | None = None) -> Path:
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = paths.backup_dir / f"state_5.sqlite.{label}.{ts}.bak"
    with connect_db(paths.db_path, readonly=True) as source, connect_db(backup_path, readonly=False) as target:
        source.backup(target)
        target.commit()
    return backup_path


def build_session_backup_dir(paths: Paths, label: str, timestamp: str) -> Path:
    return paths.backup_dir / f"sessions.{label}.{timestamp}"


def build_session_backup_target(paths: Paths, session_path: Path, backup_dir: Path) -> Path:
    try:
        relative = session_path.relative_to(paths.codex_home)
    except ValueError:
        flattened = session_path.as_posix().lstrip("/").replace("/", "__")
        relative = Path("external") / flattened
    return backup_dir / relative


def update_session_file_provider(
    paths: Paths,
    session_path: Path,
    target_provider: str,
    session_backup_dir: Path,
) -> str:
    provider, status = read_session_meta_provider(session_path)
    if status != "ok":
        return status
    if provider == target_provider:
        return "already_current"

    try:
        with session_path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            remainder = handle.read()
    except OSError:
        return "read_error"

    try:
        first_obj = json.loads(first_line)
    except json.JSONDecodeError:
        return "invalid_json"

    first_obj["payload"]["model_provider"] = target_provider
    rewritten_first_line = json.dumps(first_obj, ensure_ascii=False, separators=(",", ":")) + "\n"

    backup_target = build_session_backup_target(paths, session_path, session_backup_dir)
    backup_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(session_path, backup_target)
    except OSError:
        return "backup_error"

    temp_path = session_path.with_name(session_path.name + ".history-sync-tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rewritten_first_line)
            handle.write(remainder)
        os.replace(temp_path, session_path)
    except OSError:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return "write_error"
    return "updated"


def sync_session_files(
    paths: Paths,
    target_provider: str,
    session_backup_dir: Path,
) -> dict[str, object]:
    mode = ensure_environment(paths)
    if mode == "legacy_db":
        with connect_db(paths.db_path, readonly=True) as conn:
            rollout_paths = iter_rollout_paths(conn)
    else:
        rollout_paths = [session_path_for_thread_id(paths, row["id"]) for row in iter_session_index_rows(paths)]

    stats: dict[str, int] = {
        "total_files": len(rollout_paths),
        "updated_files": 0,
        "already_current": 0,
        "missing": 0,
        "read_error": 0,
        "empty": 0,
        "invalid_json": 0,
        "no_session_meta": 0,
        "bad_payload": 0,
        "no_provider": 0,
        "backup_error": 0,
        "write_error": 0,
    }
    touched_backup = False

    for rollout_path in rollout_paths:
        status = update_session_file_provider(paths, rollout_path, target_provider, session_backup_dir)
        if status == "updated":
            touched_backup = True
            stats["updated_files"] += 1
            continue
        if status in stats:
            stats[status] += 1

    session_backup_path = str(session_backup_dir) if touched_backup else None
    return {
        "stats": stats,
        "backup_dir": session_backup_path,
    }


def rewrite_session_meta(
    paths: Paths,
    session_path: Path,
    session_backup_dir: Path,
    transform,
) -> tuple[str, dict[str, Any] | None]:
    session_meta_obj, status = read_session_meta(session_path)
    if status != "ok" or session_meta_obj is None:
        return status, None

    try:
        with session_path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            remainder = handle.read()
    except OSError:
        return "read_error", None

    try:
        parsed_first_line = json.loads(first_line)
    except json.JSONDecodeError:
        return "invalid_json", None

    changed = transform(parsed_first_line)
    if not changed:
        return "unchanged", parsed_first_line

    backup_target = build_session_backup_target(paths, session_path, session_backup_dir)
    backup_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(session_path, backup_target)
    except OSError:
        return "backup_error", None

    temp_path = session_path.with_name(session_path.name + ".history-sync-tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(parsed_first_line, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.write(remainder)
        os.replace(temp_path, session_path)
    except OSError:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return "write_error", None
    return "updated", parsed_first_line


def read_latest_thread_name(path: Path, thread_id: str) -> str | None:
    if not path.exists():
        return None
    latest_name: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "event_msg":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "thread_name_updated":
                    continue
                payload_thread_id = payload.get("thread_id")
                if payload_thread_id not in (None, thread_id):
                    continue
                thread_name = payload.get("thread_name")
                if isinstance(thread_name, str) and thread_name.strip():
                    latest_name = thread_name.strip()
    except OSError:
        return None
    return latest_name


def repair_imported_threads(
    paths: Paths,
    target_cwd: str | None,
    thread_ids: list[str] | None = None,
    fix_rollout_prefix: bool = True,
) -> dict[str, object]:
    ensure_environment(paths)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_backup = make_backup(paths, "pre-repair", timestamp=timestamp)
    session_backup_dir = build_session_backup_dir(paths, "pre-repair", timestamp)

    with connect_db(paths.db_path, readonly=False) as conn:
        conn.row_factory = sqlite3.Row
        chosen_cwd = resolve_repair_target_cwd(conn, target_cwd)
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            query = f"SELECT * FROM threads WHERE id IN ({placeholders})"
            rows = conn.execute(query, tuple(thread_ids)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM threads").fetchall()

        repaired: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []

        for row in rows:
            session_path = resolve_local_session_path(paths, row["rollout_path"])
            original_cwd = row["cwd"]
            original_rollout_path = row["rollout_path"]
            if not thread_ids and not is_cross_device_thread(original_cwd, original_rollout_path):
                skipped.append({"id": row["id"], "title": row["title"], "reason": "not_cross_device"})
                continue
            if not session_path.exists():
                skipped.append({"id": row["id"], "title": row["title"], "reason": "missing_session_file"})
                continue

            def transform(first_obj: dict[str, Any]) -> bool:
                payload = first_obj.get("payload")
                if not isinstance(payload, dict):
                    return False
                changed = False
                if payload.get("cwd") != chosen_cwd:
                    payload["cwd"] = chosen_cwd
                    changed = True
                return changed

            session_status, _updated_meta = rewrite_session_meta(paths, session_path, session_backup_dir, transform)
            normalized_rollout_path = normalize_local_path(session_path) if fix_rollout_prefix else str(session_path)
            needs_db_update = original_cwd != chosen_cwd or (fix_rollout_prefix and original_rollout_path != normalized_rollout_path)

            if session_status in {"updated", "unchanged"} and needs_db_update:
                now_s = int(datetime.now().timestamp())
                now_ms = int(datetime.now().timestamp() * 1000)
                conn.execute(
                    """
                    UPDATE threads
                    SET cwd = ?, rollout_path = ?, updated_at = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (chosen_cwd, normalized_rollout_path, now_s, now_ms, row["id"]),
                )
                repaired.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "session_status": session_status,
                        "cwd_before": original_cwd,
                        "cwd_after": chosen_cwd,
                        "rollout_path_before": original_rollout_path,
                        "rollout_path_after": normalized_rollout_path,
                    }
                )
            elif session_status in {"updated", "unchanged"}:
                skipped.append({"id": row["id"], "title": row["title"], "reason": "already_normalized"})
            else:
                skipped.append({"id": row["id"], "title": row["title"], "reason": session_status})

        conn.commit()

    status_after = get_status(paths)
    return {
        "action": "repair",
        "db_backup": str(db_backup),
        "session_backup_dir": str(session_backup_dir),
        "target_cwd": chosen_cwd,
        "repaired_threads": repaired,
        "skipped_threads": skipped,
        "status_after": status_after,
    }


def list_threads(paths: Paths, limit: int = 200) -> dict[str, object]:
    mode = ensure_environment(paths)
    threads: list[dict[str, object]] = []

    if mode == "legacy_db":
        with connect_db(paths.db_path, readonly=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, title, cwd, rollout_path, updated_at, updated_at_ms
                FROM threads
                ORDER BY updated_at DESC, updated_at_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        for row in rows:
            jsonl_title = read_latest_thread_name(resolve_local_session_path(paths, row["rollout_path"]), row["id"])
            display_title = jsonl_title or row["title"]
            threads.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "display_title": display_title,
                    "title_source": "jsonl:thread_name_updated" if jsonl_title else "threads.title",
                    "cwd": row["cwd"],
                    "rollout_path": row["rollout_path"],
                    "updated_at": row["updated_at"],
                    "updated_at_ms": row["updated_at_ms"],
                }
            )
    else:
        rows = sorted(
            iter_session_index_rows(paths),
            key=lambda item: iso_to_epoch_seconds(str(item.get("updated_at") or "")),
            reverse=True,
        )[:limit]
        for row in rows:
            thread_id = str(row["id"])
            session_path = session_path_for_thread_id(paths, thread_id)
            session_meta_obj, status = read_session_meta(session_path)
            payload = session_meta_obj.get("payload", {}) if status == "ok" and session_meta_obj else {}
            cwd = payload.get("cwd", "")
            raw_title = str(row.get("thread_name") or payload.get("title") or thread_id)
            jsonl_title = read_latest_thread_name(session_path, thread_id)
            display_title = jsonl_title or raw_title
            updated_at = iso_to_epoch_seconds(str(row.get("updated_at") or ""))
            threads.append(
                {
                    "id": thread_id,
                    "title": raw_title,
                    "display_title": display_title,
                    "title_source": "jsonl:thread_name_updated" if jsonl_title else "session_index.thread_name",
                    "cwd": cwd,
                    "rollout_path": str(session_path),
                    "updated_at": updated_at,
                    "updated_at_ms": updated_at * 1000,
                }
            )

    return {
        "action": "list-threads",
        "threads": threads,
    }


def list_cwds(paths: Paths) -> dict[str, object]:
    ensure_environment(paths)
    with connect_db(paths.db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT cwd, COUNT(*) AS thread_count, MAX(updated_at) AS last_updated_at
            FROM threads
            WHERE cwd IS NOT NULL
              AND cwd <> ''
            GROUP BY cwd
            ORDER BY last_updated_at DESC, cwd ASC
            """
        ).fetchall()

    return {
        "action": "list-cwds",
        "cwds": [
            {
                "cwd": cwd,
                "thread_count": int(thread_count),
                "last_updated_at": int(last_updated_at or 0),
            }
            for cwd, thread_count, last_updated_at in rows
        ],
    }


def move_threads_to_cwd(paths: Paths, thread_ids: list[str], target_cwd: str) -> dict[str, object]:
    ensure_environment(paths)
    if not thread_ids:
        raise RuntimeError("At least one thread id is required.")
    if not target_cwd or not target_cwd.strip():
        raise RuntimeError("Target cwd is required.")

    normalized_cwd = normalize_explicit_cwd(target_cwd.strip())
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_backup = make_backup(paths, "pre-move", timestamp=timestamp)
    session_backup_dir = build_session_backup_dir(paths, "pre-move", timestamp)

    with connect_db(paths.db_path, readonly=False) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in thread_ids)
        rows = conn.execute(f"SELECT * FROM threads WHERE id IN ({placeholders})", tuple(thread_ids)).fetchall()
        found_ids = {row["id"] for row in rows}
        missing_ids = [thread_id for thread_id in thread_ids if thread_id not in found_ids]
        if missing_ids:
            raise RuntimeError(f"Thread id not found: {', '.join(missing_ids)}")

        moved: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []

        for row in rows:
            session_path = resolve_local_session_path(paths, row["rollout_path"])
            if not session_path.exists():
                skipped.append({"id": row["id"], "title": row["title"], "reason": "missing_session_file"})
                continue

            def transform(first_obj: dict[str, Any]) -> bool:
                payload = first_obj.get("payload")
                if not isinstance(payload, dict):
                    return False
                if payload.get("cwd") == normalized_cwd:
                    return False
                payload["cwd"] = normalized_cwd
                return True

            session_status, _updated_meta = rewrite_session_meta(paths, session_path, session_backup_dir, transform)
            if session_status not in {"updated", "unchanged"}:
                skipped.append({"id": row["id"], "title": row["title"], "reason": session_status})
                continue

            old_cwd = row["cwd"]
            if old_cwd != normalized_cwd:
                now_s = int(datetime.now().timestamp())
                now_ms = int(datetime.now().timestamp() * 1000)
                conn.execute(
                    """
                    UPDATE threads
                    SET cwd = ?, updated_at = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (normalized_cwd, now_s, now_ms, row["id"]),
                )
                moved.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "cwd_before": old_cwd,
                        "cwd_after": normalized_cwd,
                        "session_status": session_status,
                    }
                )
            else:
                skipped.append({"id": row["id"], "title": row["title"], "reason": "already_in_target_cwd"})

        conn.commit()

    return {
        "action": "move-thread",
        "target_cwd": normalized_cwd,
        "db_backup": str(db_backup),
        "session_backup_dir": str(session_backup_dir),
        "moved_threads": moved,
        "skipped_threads": skipped,
        "status_after": get_status(paths),
    }


def checkpoint(conn: sqlite3.Connection) -> tuple[int, int, int]:
    row = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def sync_to_current_provider(paths: Paths, sync_sessions: bool = True) -> dict[str, object]:
    status_before = get_status(paths)
    current_provider = str(status_before["current_provider"])
    return sync_to_target_provider(paths, current_provider, sync_sessions=sync_sessions, status_before=status_before)


def sync_to_target_provider(
    paths: Paths,
    target_provider: str,
    sync_sessions: bool = True,
    status_before: dict[str, object] | None = None,
) -> dict[str, object]:
    mode = ensure_environment(paths)
    normalized_target_provider = normalize_provider_name(target_provider)
    if status_before is None:
        status_before = get_status(paths)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path: str | None

    if mode == "legacy_db":
        backup_path = str(make_backup(paths, "pre-sync", timestamp=timestamp))
        with connect_db(paths.db_path, readonly=False) as conn:
            before_counts = query_provider_counts(conn)
            updated_rows = conn.execute(
                "UPDATE threads SET model_provider = ? WHERE model_provider <> ?",
                (normalized_target_provider, normalized_target_provider),
            ).rowcount
            conn.commit()
            checkpoint_result = checkpoint(conn)
            after_counts = query_provider_counts(conn)
    else:
        before_counts = query_provider_counts_from_session_index(paths)
        updated_rows = 0
        checkpoint_result = (0, 0, 0)
        backup_path = None

    session_sync_payload: dict[str, object]
    if sync_sessions:
        session_backup_dir = build_session_backup_dir(paths, "pre-sync", timestamp)
        session_sync_payload = sync_session_files(paths, normalized_target_provider, session_backup_dir)
    else:
        session_sync_payload = {"skipped": True}

    if mode == "session_files":
        after_counts = query_provider_counts_from_session_index(paths)

    status_after = get_status(paths)

    return {
        "action": "sync",
        "target_provider": normalized_target_provider,
        "status_before": status_before,
        "updated_rows": updated_rows,
        "backup_path": backup_path,
        "before_counts": [{"provider": key, "count": value} for key, value in before_counts.items()],
        "after_counts": [{"provider": key, "count": value} for key, value in after_counts.items()],
        "checkpoint": {
            "busy": checkpoint_result[0],
            "log_frames": checkpoint_result[1],
            "checkpointed_frames": checkpoint_result[2],
        },
        "session_sync": session_sync_payload,
        "status_after": status_after,
    }


def resolve_backup(paths: Paths, requested_path: str | None) -> Path:
    if requested_path:
        backup = Path(requested_path).expanduser()
    else:
        backups = list_backups(paths, limit=1)
        if not backups:
            raise RuntimeError("No backup files were found.")
        backup = Path(backups[0]["path"])
    if not backup.exists():
        raise RuntimeError(f"Backup file does not exist: {backup}")
    return backup


def restore_backup(paths: Paths, backup_path: str | None) -> dict[str, object]:
    ensure_environment(paths)
    chosen_backup = resolve_backup(paths, backup_path)
    restore_snapshot = make_backup(paths, "pre-restore")

    with connect_db(chosen_backup, readonly=True) as source, connect_db(paths.db_path, readonly=False) as target:
        source.backup(target)
        checkpoint_result = checkpoint(target)

    status_after = get_status(paths)
    return {
        "action": "restore",
        "restored_from": str(chosen_backup),
        "safety_backup": str(restore_snapshot),
        "checkpoint": {
            "busy": checkpoint_result[0],
            "log_frames": checkpoint_result[1],
            "checkpointed_frames": checkpoint_result[2],
        },
        "status": status_after,
    }


def to_json(payload: dict[str, object]) -> str:
    # Keep stdout ASCII-only so Windows PowerShell code-page decoding cannot
    # corrupt non-ASCII paths before ConvertFrom-Json sees them.
    return json.dumps(payload, ensure_ascii=True, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex history sync helper")
    parser.add_argument("--codex-home", help="Override Codex home directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show current provider/thread status")
    list_threads_parser = subparsers.add_parser("list-threads", help="List recent threads for manual classification")
    list_threads_parser.add_argument("--limit", type=int, default=200, help="Maximum number of threads to list")
    subparsers.add_parser("list-cwds", help="List existing thread working directories")
    sync_parser = subparsers.add_parser("sync", help="Move all thread providers to the current provider")
    sync_parser.add_argument(
        "--target-provider",
        help="Explicit target provider to sync into; when omitted the tool infers the current active provider",
    )
    sync_parser.add_argument(
        "--skip-session-files",
        action="store_true",
        help="Only update SQLite threads table and skip session jsonl provider updates",
    )
    restore_parser = subparsers.add_parser("restore", help="Restore from a backup")
    restore_parser.add_argument("--backup", help="Backup file path; newest backup is used when omitted")
    subparsers.add_parser("backup", help="Create a manual backup")
    repair_parser = subparsers.add_parser("repair", help="Repair imported thread metadata for cross-device use")
    repair_parser.add_argument("--cwd", help="Target working directory to write into imported sessions")
    repair_parser.add_argument("--thread-id", action="append", dest="thread_ids", help="Specific thread id to repair; can be repeated")
    move_parser = subparsers.add_parser("move-thread", help="Move one or more threads to a target cwd")
    move_parser.add_argument("--cwd", required=True, help="Target working directory")
    move_parser.add_argument("--thread-id", action="append", dest="thread_ids", required=True, help="Thread id to move; can be repeated")

    args = parser.parse_args()
    paths = resolve_paths(args.codex_home)

    try:
        if args.command == "status":
            payload = get_status(paths)
        elif args.command == "list-threads":
            payload = list_threads(paths, limit=args.limit)
        elif args.command == "list-cwds":
            payload = list_cwds(paths)
        elif args.command == "sync":
            if args.target_provider:
                payload = sync_to_target_provider(
                    paths,
                    target_provider=args.target_provider,
                    sync_sessions=not args.skip_session_files,
                )
            else:
                payload = sync_to_current_provider(paths, sync_sessions=not args.skip_session_files)
        elif args.command == "restore":
            payload = restore_backup(paths, args.backup)
        elif args.command == "backup":
            ensure_environment(paths)
            payload = {"action": "backup", "backup_path": str(make_backup(paths, "manual"))}
        elif args.command == "repair":
            payload = repair_imported_threads(paths, target_cwd=args.cwd, thread_ids=args.thread_ids)
        elif args.command == "move-thread":
            payload = move_threads_to_cwd(paths, thread_ids=args.thread_ids, target_cwd=args.cwd)
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        error_payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(to_json(error_payload))
        else:
            print(error_payload["error"])
        return 1

    if isinstance(payload, dict):
        payload["ok"] = True

    if args.json:
        print(to_json(payload))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
