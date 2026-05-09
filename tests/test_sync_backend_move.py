from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sync_backend


class MoveThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        self.sessions_dir = self.codex_home / "sessions"
        self.sessions_dir.mkdir()
        self.db_path = self.codex_home / "state_5.sqlite"
        self.config_path = self.codex_home / "config.toml"
        self.config_path.write_text('model_provider = "OpenAI"\nmodel = "gpt-5.5"\n', encoding="utf-8")
        self.paths = sync_backend.resolve_paths(str(self.codex_home))

        self.thread_id = "019ddd16-0453-7d62-8868-4979301b62ef"
        self.session_path = self.sessions_dir / "rollout-test.jsonl"
        self.session_path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": self.thread_id,
                        "cwd": "/old/project",
                        "model_provider": "OpenAI",
                    },
                }
            )
            + "\n"
            + json.dumps({"type": "turn", "payload": {"text": "hello"}})
            + "\n",
            encoding="utf-8",
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    has_user_event INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER,
                    git_sha TEXT,
                    git_branch TEXT,
                    git_origin_url TEXT,
                    cli_version TEXT NOT NULL DEFAULT '',
                    first_user_message TEXT NOT NULL DEFAULT '',
                    agent_nickname TEXT,
                    agent_role TEXT,
                    memory_mode TEXT NOT NULL DEFAULT 'enabled',
                    model TEXT,
                    reasoning_effort TEXT,
                    agent_path TEXT,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER,
                    thread_source TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode
                )
                VALUES (?, ?, 1, 1, 'codex', 'OpenAI', ?, '流媒体现场实验', 'workspace-write', 'on-request')
                """,
                (self.thread_id, str(self.session_path), "/old/project"),
            )
            conn.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode
                )
                VALUES (?, ?, 2, 2, 'codex', 'OpenAI', ?, '公共空间会话', 'workspace-write', 'on-request')
                """,
                ("public-thread", str(self.session_path), "/Users/lyl/Documents/Codex"),
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_move_thread_updates_sqlite_and_session_meta_cwd(self) -> None:
        result = sync_backend.move_threads_to_cwd(
            self.paths,
            thread_ids=[self.thread_id],
            target_cwd="/Users/lyl/Documents/Codex",
        )

        self.assertEqual(result["moved_threads"][0]["id"], self.thread_id)
        with sqlite3.connect(self.db_path) as conn:
            cwd = conn.execute("SELECT cwd FROM threads WHERE id = ?", (self.thread_id,)).fetchone()[0]
        self.assertEqual(cwd, "/Users/lyl/Documents/Codex")

        first_line = self.session_path.read_text(encoding="utf-8").splitlines()[0]
        session_meta = json.loads(first_line)
        self.assertEqual(session_meta["payload"]["cwd"], "/Users/lyl/Documents/Codex")

    def test_move_thread_rejects_unknown_thread_id(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Thread id not found"):
            sync_backend.move_threads_to_cwd(
                self.paths,
                thread_ids=["missing-thread"],
                target_cwd="/Users/lyl/Documents/Codex",
            )

    def test_list_cwds_returns_existing_workspaces_with_counts(self) -> None:
        result = sync_backend.list_cwds(self.paths)

        rows = result["cwds"]
        self.assertEqual(rows[0]["cwd"], "/Users/lyl/Documents/Codex")
        self.assertEqual(rows[0]["thread_count"], 1)
        self.assertEqual(rows[1]["cwd"], "/old/project")
        self.assertEqual(rows[1]["thread_count"], 1)

    def test_list_threads_prefers_jsonl_thread_name_updated_for_display_title(self) -> None:
        with self.session_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": self.thread_id,
                            "thread_name": "修复跨设备同步",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        result = sync_backend.list_threads(self.paths)
        row = next(item for item in result["threads"] if item["id"] == self.thread_id)

        self.assertEqual(row["title"], "流媒体现场实验")
        self.assertEqual(row["display_title"], "修复跨设备同步")
        self.assertEqual(row["title_source"], "jsonl:thread_name_updated")

    def test_get_status_falls_back_to_latest_thread_provider_when_config_has_no_model_provider(self) -> None:
        self.config_path.write_text('model = "gpt-5.5"\n', encoding="utf-8")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE threads
                SET model_provider = ?, updated_at = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                ("openai", 99, 99, "public-thread"),
            )

        status = sync_backend.get_status(self.paths)

        self.assertEqual(status["current_provider"], "openai")
        self.assertEqual(status["movable_threads"], 1)

    def test_get_status_keeps_configured_provider_case(self) -> None:
        status = sync_backend.get_status(self.paths)

        self.assertEqual(status["current_provider"], "OpenAI")
        self.assertEqual(status["current_provider_source"], "config:model_provider")
        self.assertEqual(status["current_provider_kind"], "third_party")

    def test_sync_to_explicit_target_provider_updates_sqlite_and_session_meta(self) -> None:
        result = sync_backend.sync_to_target_provider(
            self.paths,
            target_provider="anthropic",
            sync_sessions=True,
        )

        self.assertEqual(result["target_provider"], "anthropic")
        self.assertEqual(result["updated_rows"], 2)

        with sqlite3.connect(self.db_path) as conn:
            providers = [
                row[0]
                for row in conn.execute(
                    "SELECT model_provider FROM threads ORDER BY id ASC"
                ).fetchall()
            ]
        self.assertEqual(providers, ["anthropic", "anthropic"])

        first_line = self.session_path.read_text(encoding="utf-8").splitlines()[0]
        session_meta = json.loads(first_line)
        self.assertEqual(session_meta["payload"]["model_provider"], "anthropic")

    def test_provider_kind_treats_only_lowercase_openai_as_official(self) -> None:
        self.assertEqual(sync_backend.classify_provider_kind("openai"), "official")
        self.assertEqual(sync_backend.classify_provider_kind("OpenAI"), "third_party")
        self.assertEqual(sync_backend.classify_provider_kind("custom"), "third_party")


class ModernStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        self.sessions_dir = self.codex_home / "sessions" / "2026" / "05" / "09"
        self.sessions_dir.mkdir(parents=True)
        self.sqlite_dir = self.codex_home / "sqlite"
        self.sqlite_dir.mkdir()
        self.config_path = self.codex_home / "config.toml"
        self.config_path.write_text('model = "gpt-5.5"\n', encoding="utf-8")
        (self.codex_home / "auth.json").write_text('{"logged_in": true}\n', encoding="utf-8")
        (self.sqlite_dir / "codex-dev.db").touch()

        self.official_id = "official-thread"
        self.third_party_id = "third-party-thread"

        self.official_session_path = self.sessions_dir / "rollout-official.jsonl"
        self.third_party_session_path = self.sessions_dir / "rollout-third-party.jsonl"

        self.official_session_path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": self.official_id,
                        "cwd": "/official/project",
                        "model_provider": "openai",
                        "timestamp": "2026-05-09T07:05:14.965Z",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_name_updated",
                        "thread_id": self.official_id,
                        "thread_name": "官方会话",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.third_party_session_path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": self.third_party_id,
                        "cwd": "/third/project",
                        "model_provider": "OpenAI",
                        "timestamp": "2026-05-09T07:06:14.965Z",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_name_updated",
                        "thread_id": self.third_party_id,
                        "thread_name": "第三方会话",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        (self.codex_home / "session_index.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": self.official_id,
                            "thread_name": "官方会话",
                            "updated_at": "2026-05-09T07:05:27.312658Z",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "id": self.third_party_id,
                            "thread_name": "第三方会话",
                            "updated_at": "2026-05-09T07:06:27.312658Z",
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.paths = sync_backend.resolve_paths(str(self.codex_home))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_status_reads_modern_storage_without_state_db(self) -> None:
        status = sync_backend.get_status(self.paths)

        self.assertEqual(status["current_provider"], "openai")
        self.assertEqual(status["current_provider_source"], "auth.json")
        self.assertEqual(status["total_threads"], 2)
        self.assertEqual(status["movable_threads"], 1)
        self.assertEqual(status["storage_mode"], "session_files")
        self.assertEqual(
            [(row["provider"], row["count"]) for row in status["provider_counts"]],
            [("OpenAI", 1), ("openai", 1)],
        )

    def test_list_threads_reads_titles_and_order_from_modern_storage(self) -> None:
        result = sync_backend.list_threads(self.paths)

        self.assertEqual(result["threads"][0]["id"], self.third_party_id)
        self.assertEqual(result["threads"][0]["display_title"], "第三方会话")
        self.assertEqual(result["threads"][1]["id"], self.official_id)
        self.assertEqual(result["threads"][1]["display_title"], "官方会话")

    def test_sync_updates_session_meta_without_state_db(self) -> None:
        result = sync_backend.sync_to_target_provider(
            self.paths,
            target_provider="custom",
            sync_sessions=True,
        )

        self.assertEqual(result["target_provider"], "custom")
        self.assertEqual(result["updated_rows"], 0)
        self.assertEqual(result["session_sync"]["stats"]["updated_files"], 2)
        self.assertEqual(result["status_after"]["current_provider"], "openai")
        self.assertEqual(
            [(row["provider"], row["count"]) for row in result["status_after"]["provider_counts"]],
            [("custom", 2)],
        )

        official_meta = json.loads(self.official_session_path.read_text(encoding="utf-8").splitlines()[0])
        third_party_meta = json.loads(self.third_party_session_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(official_meta["payload"]["model_provider"], "custom")
        self.assertEqual(third_party_meta["payload"]["model_provider"], "custom")


if __name__ == "__main__":
    unittest.main()
