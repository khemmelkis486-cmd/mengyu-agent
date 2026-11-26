"""Task Center parser and SQLite persistence regression tests."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parser = load_module("mengyu_task_parser_test", ROOT / "parser.py")
store_module = load_module("mengyu_task_store_test", ROOT / "store.py")


class ParserTests(unittest.TestCase):
    def test_explicit_promise_is_captured(self):
        parsed = parser.parse_commitment("我答应陪你看电影", speaker="user")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["classification"], "explicit")

    def test_ambiguous_promise_requires_confirmation(self):
        parsed = parser.parse_commitment("有空陪你看电影", speaker="user")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["classification"], "ambiguous")

    def test_past_purchase_is_progress_not_a_new_promise(self):
        self.assertIsNone(parser.parse_commitment("我已经买好了耳机", speaker="user"))
        self.assertEqual(parser.commitment_progress("耳机已经买好了"), "purchased")

    def test_short_confirmation_only(self):
        self.assertTrue(parser.confirmation_intent("嗯嗯"))
        self.assertFalse(parser.confirmation_intent("不是，我开玩笑的"))
        self.assertIsNone(parser.confirmation_intent("嗯嗯，我觉得蓝色更好看"))


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_module.TaskStore(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_must_be_prompted_before_short_answer_can_be_used(self):
        candidate, created = self.store.add_candidate(
            text="有空陪你看电影", title="一起看电影", kind="shared_experience",
            owner="user", beneficiary="mengyu", confidence=0.76,
            reason="时间不明确", source_id="user:1", now=1000,
        )
        self.assertTrue(created)
        self.assertIsNone(candidate.get("prompted_at"))
        self.assertTrue(self.store.mark_candidate_prompted(candidate["id"], now=1100))
        pending = self.store.pending_candidates(now=1200, limit=1)[0]
        self.assertEqual(pending["prompted_at"], 1100)
        resolved = self.store.resolve_candidate(candidate["id"], True, now=1200)
        self.assertTrue(resolved["accepted"])
        self.assertIsNotNone(self.store.get_task(resolved["task_id"]))

    def test_commitment_status_cannot_roll_back_without_explicit_correction(self):
        commitment, created = self.store.create_commitment(
            title="陪你看电影", original_quote="我周末陪你看电影",
            promisor="user", promisee="mengyu", source_id="u1", now=1000,
        )
        self.assertTrue(created)
        self.store.update_commitment(commitment["id"], "purchased", evidence="已买票", now=1100)
        current = self.store.update_commitment(commitment["id"], "promised", evidence="旧消息", now=1200)
        self.assertEqual(current["status"], "purchased")

    def test_task_completion_requires_evidence(self):
        task, _ = self.store.create_task(
            task_type="personal", title="整理资料", owner="mengyu",
            beneficiary="mengyu", source="test", now=1000,
        )
        with self.assertRaises(ValueError):
            self.store.update_task(task["id"], status="completed", now=1100)
        completed = self.store.update_task(task["id"], status="completed", evidence="文件已保存", now=1200)
        self.assertEqual(completed["status"], "completed")

    def test_legacy_work_complete_without_delivered_artifact_is_not_completed(self):
        row = self.store.sync_work_order({
            "id": "work-legacy", "brief": "旧海报", "stage": "完成",
            "worked_seconds": 10000, "artifacts": [],
        }, now=1000)
        self.assertEqual(row["status"], "review")

    def test_work_sync_completes_only_with_delivered_local_artifact_evidence(self):
        artifact_path = Path(self.temp.name) / "poster.png"
        artifact_path.write_bytes(b"test-image")
        order = {
            "id": "work-verified", "brief": "活动海报", "stage": "完成",
            "worked_seconds": 10000,
            "completion_evidence": {"artifact_id": "img-1", "uri": str(artifact_path)},
            "artifacts": [{"id": "img-1", "kind": "image", "status": "delivered", "uri": str(artifact_path)}],
        }
        row = self.store.sync_work_order(order, now=2000)
        self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()
