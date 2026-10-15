import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest

import collector
import remote_reader

SINCE = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def event(kind, payload, at="2026-10-14T10:00:00Z"):
    return {"timestamp": at, "type": kind, "payload": payload}


def record(response="resp_1", value=100):
    return {"response_id": response, "session_id": "s1", "thread_id": "s1", "turn_id": "t1",
            "usage": {"input_tokens": value, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 5}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.file = self.logs / "session.jsonl"
        self.db = collector.connect(self.root / "state")
        self.prefix = [event("session_meta", {"id": "s1", "model_provider": "openai"}),
                       event("turn_context", {"turn_id": "t1", "model": "gpt-6-astra", "effort": "high"})]

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def write(self, rows):
        self.file.write_text("".join(json.dumps(r) + "\n" for r in rows))
        fixed = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.file, (fixed, fixed))

    def read(self, cursors=None):
        return remote_reader.scan({"since": SINCE.isoformat(), "cursors": cursors or {}}, self.logs)

    def test_replayed_response_and_compaction_are_counted_once(self):
        request = record()
        self.write(self.prefix + [event("token_usage_record", request),
                                 event("compacted", {"latest_token_usage_record": request}),
                                 event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 999999999}}})])
        scan = self.read()
        collector.apply_scan(self.db, "alpha", scan)
        collector.apply_scan(self.db, "alpha", scan)
        rows = collector.pending(self.db, "alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["in"], rows[0]["cache_read"], rows[0]["out"]), (20, 80, 10))
        self.assertEqual(rows[0]["reasoning"], 5)  # Already part of output, never added twice.

    def test_incomplete_line_waits_then_is_imported_once(self):
        self.write(self.prefix)
        tail = json.dumps(event("token_usage_record", record())).encode()
        with self.file.open("ab") as file:
            file.write(tail[:30])
        scan = self.read()
        collector.apply_scan(self.db, "alpha", scan)
        self.assertEqual(collector.pending(self.db, "alpha"), [])
        with self.file.open("ab") as file:
            file.write(tail[30:] + b"\n")
        scan = self.read(scan["cursors"])
        collector.apply_scan(self.db, "alpha", scan)
        self.assertEqual(len(collector.pending(self.db, "alpha")), 1)
        self.assertEqual(self.read(scan["cursors"])["bytes_read"], 0)

    def test_replaced_log_replays_without_duplicate_usage(self):
        self.write(self.prefix + [event("token_usage_record", record())])
        first = self.read()
        collector.apply_scan(self.db, "alpha", first)
        self.file.rename(self.root / "previous.jsonl")
        self.write(self.prefix + [event("token_usage_record", record()), event("token_usage_record", record("resp_2"))])
        collector.apply_scan(self.db, "alpha", self.read(first["cursors"]))
        self.assertEqual(len(collector.pending(self.db, "alpha")), 2)

    def test_conflicting_request_rolls_back_and_does_not_advance_cursor(self):
        self.write(self.prefix + [event("token_usage_record", record())])
        first = self.read()
        collector.apply_scan(self.db, "alpha", first)
        with self.file.open("a") as file:
            file.write(json.dumps(event("token_usage_record", record(value=101))) + "\n")
        second = self.read(first["cursors"])
        with self.assertRaisesRegex(ValueError, "发生修订"):
            collector.apply_scan(self.db, "alpha", second)
        stored = json.loads(self.db.execute("SELECT body FROM cursors").fetchone()[0])
        self.assertEqual(stored["offset"], first["cursors"]["session.jsonl"]["offset"])

    def test_missing_model_is_held_without_inference(self):
        self.write([self.prefix[0], event("token_usage_record", record())])
        collector.apply_scan(self.db, "alpha", self.read())
        self.assertEqual(collector.pending(self.db, "alpha"), [])

    def test_unknown_counter_is_rejected(self):
        for bad in (-1, 1.5, True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                collector.counts({"input_tokens": bad, "output_tokens": 1})

    def test_cutoff_uses_event_time_and_keeps_context(self):
        self.write(self.prefix + [event("token_usage_record", record("old"), "2026-09-01T00:00:00Z"),
                                 event("token_usage_record", record("new"))])
        collector.apply_scan(self.db, "alpha", self.read())
        rows = collector.pending(self.db, "alpha")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["rid"].endswith(":new"))

    def test_two_servers_keep_identical_response_ids_separate(self):
        self.write(self.prefix + [event("token_usage_record", record())])
        scan = self.read()
        collector.apply_scan(self.db, "alpha", scan)
        beta = collector.connect(self.root / "beta-state")
        try:
            collector.apply_scan(beta, "beta", scan)
            old, new = collector.pending(self.db, "alpha")[0], collector.pending(beta, "beta")[0]
            self.assertNotEqual(old["rid"], new["rid"])
            self.assertEqual((old["agent"], new["agent"]), ("SSH-alpha", "SSH-beta"))
            self.assertEqual((old["session"], new["session"]), ("alpha/s1", "beta/s1"))
        finally:
            beta.close()

    def test_state_directory_cannot_be_reused_for_another_server(self):
        self.write(self.prefix + [event("token_usage_record", record())])
        scan = self.read()
        collector.apply_scan(self.db, "alpha", scan)
        with self.assertRaisesRegex(ValueError, "属于另一台服务器"):
            collector.apply_scan(self.db, "beta", scan)

    def test_subagent_model_uses_executing_thread_not_parent_session(self):
        request = record()
        request.update(thread_id="child", turn_id="child-turn")
        self.write(self.prefix + [event("token_usage_record", request)])
        child = [event("session_meta", {"id": "child", "model_provider": "openai"}),
                 event("turn_context", {"turn_id": "child-turn", "model": "codex-auto-review"})]
        (self.logs / "child.jsonl").write_text("".join(json.dumps(row) + "\n" for row in child))
        fixed = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.logs / "child.jsonl", (fixed, fixed))
        collector.apply_scan(self.db, "beta", self.read())
        rows = collector.pending(self.db, "beta")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "codex-auto-review")
        self.assertEqual(rows[0]["session"], "beta/child")

    def test_remote_export_drops_unrecognized_and_sensitive_fields(self):
        request = record()
        request.update(prompt="PRIVATE_PROMPT", api_key="PRIVATE_KEY")
        self.write(self.prefix + [event("token_usage_record", request),
            event("event_msg", {"type": "token_count", "rate_limits": {
                "limit_id": "codex", "account_email": "PRIVATE_ACCOUNT", "credits": {"balance": "PRIVATE_BALANCE"},
                "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 1800000000, "token": "PRIVATE_TOKEN"}}})])
        scan = self.read()
        serialized = json.dumps(scan)
        self.assertNotIn("PRIVATE_", serialized)
        self.assertTrue(any(row["kind"] == "quota" for row in scan["events"]))


if __name__ == "__main__":
    unittest.main()
