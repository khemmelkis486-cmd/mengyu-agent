import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "mengyu_fact_validator_tests"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault(PKG, pkg)
validator = importlib.import_module(f"{PKG}.fact_validator")


def projection(activity=None, completed=None, paused=None, conflicts=None):
    return {
        "mengyu": {
            "activity": activity,
            "recent_completed": completed or [],
            "recently_paused": paused or [],
        },
        "consistency": {"conflicts": conflicts or []},
    }


class EvidenceValidatorTests(unittest.TestCase):
    def test_subjective_and_intention_are_not_claims(self):
        p = projection({"name": "在餐馆吃饭", "place": "餐馆"})
        for text in ("我现在觉得有点累。", "我等会儿想去厨房炒菜。", "我准备去逛街。"):
            cleaned, issues = validator.validate(text, p)
            self.assertEqual(cleaned, text)
            self.assertEqual(issues, [])

    def test_recorded_fictional_current_activity_is_supported(self):
        p = projection({"name": "在家画画", "place": "家里", "fictional": True})
        issues = validator.inspect_claims("我现在在家画画。", p)
        self.assertTrue(any(x["grade"] == validator.SUPPORTED for x in issues))

    def test_paused_activity_cannot_support_current_activity_claim(self):
        p = projection(
            {"name": "在餐馆吃饭", "place": "餐馆", "status": "in_progress"},
            paused=[{"name": "在厨房炒菜", "place": "厨房", "status": "paused"}],
        )
        cleaned, issues = validator.validate("我现在在厨房炒菜。", p)
        self.assertEqual(cleaned, "我现在在餐馆。")
        self.assertTrue(any(i["grade"] == validator.CONTRADICTED for i in issues))

    def test_conflicting_current_name_and_destination_does_not_invent_location(self):
        p = projection({"name": "附近餐馆", "place": "住处", "status": "in_progress"})
        original = "我现在在厨房炒菜。"
        cleaned, issues = validator.validate(original, p)
        self.assertEqual(cleaned, original)
        self.assertTrue(any(i["reason"] == "conflicting_current_activity_location_sources"
                            for i in issues))

    def test_recent_completed_activity_supports_past_claim(self):
        p = projection(completed=[{"name": "在厨房炒菜", "place": "厨房", "status": "completed"}])
        issues = validator.inspect_claims("我刚才在厨房炒菜。", p)
        self.assertTrue(any(x["grade"] == validator.SUPPORTED for x in issues))

    def test_high_confidence_current_location_conflict_is_corrected_locally(self):
        p = projection({"name": "在餐馆吃饭", "place": "餐馆"})
        original = "我现在在厨房炒菜，盐好像放多了。"
        cleaned, issues = validator.validate(original, p)
        self.assertEqual(cleaned, "我现在在餐馆，盐好像放多了。")
        self.assertTrue(any(x["grade"] == validator.CONTRADICTED and x["corrected"] for x in issues))

    def test_unresolved_projection_conflict_does_not_force_correction(self):
        p = projection({"name": "在餐馆吃饭", "place": "餐馆"}, conflicts=[{"type": "multiple_active_activities"}])
        original = "我现在在厨房炒菜。"
        cleaned, issues = validator.validate(original, p)
        self.assertEqual(cleaned, original)
        self.assertTrue(any(x["grade"] == validator.UNSUPPORTED for x in issues))

    def test_unsupported_owner_action_is_softened_to_question_not_deleted(self):
        p = projection()
        cleaned, issues = validator.validate("你今天点了肯德基。", p, owner_texts=["你觉得肯德基怎么样？"])
        self.assertEqual(cleaned, "你是不是今天点了肯德基？")
        self.assertTrue(any(x["kind"] == "owner_action" and x["grade"] == validator.UNSUPPORTED for x in issues))

    def test_user_message_with_completed_action_supports_owner_claim(self):
        p = projection()
        issues = validator.inspect_claims("你今天点了肯德基。", p, owner_texts=["我今天已经点了肯德基。"])
        self.assertTrue(any(x["grade"] == validator.SUPPORTED for x in issues))

    def test_question_is_not_rewritten(self):
        p = projection()
        original = "你今天点了肯德基吗？"
        cleaned, issues = validator.validate(original, p)
        self.assertEqual(cleaned, original)
        self.assertEqual(issues, [])

    def test_unsupported_self_claim_is_reported_not_deleted(self):
        p = projection()
        original = "我现在在厨房炒菜，今天心情还不错。"
        cleaned, issues = validator.validate(original, p)
        self.assertEqual(cleaned, original)
        self.assertTrue(any(x["grade"] == validator.UNSUPPORTED for x in issues))


if __name__ == "__main__":
    unittest.main()
