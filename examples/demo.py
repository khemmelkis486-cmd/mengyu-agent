"""Replay four isolated decisions using public, synthetic data."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace


PLUGINS = Path(__file__).resolve().parents[1] / "plugins"
sys.path.insert(0, str(PLUGINS))

from mengyu_contact import contact_sources, delivery_state, fact_validator, outbound_guard


def main():
    choices = contact_sources.available_contact_sources("spontaneous_share")
    motive, event_id, _ = contact_sources.resolve_contact_source(
        "", choices, message="突然有点想你了。"
    )

    draft = "我现在在厨房炒菜。"
    projection = {"mengyu": {"activity": {"name": "在餐馆吃饭", "place": "餐馆"}}}
    revision, findings = fact_validator.validate(draft, projection)

    outgoing = [SimpleNamespace(text="照片拍好了，我发你看看。")]
    photo_accepted, reason = outbound_guard.check(outgoing, PLUGINS)

    delivery = {}
    delivery_state.confirm(delivery, 0, "我到餐馆了。", 100, receipt="demo-receipt")
    delivery_state.mark(delivery, 1, "reserved", 101)
    delivery_state.recover(delivery, 102)

    print(json.dumps({
        "主动联系": {"候选动机": choices, "示例选择": motive, "活动事件 ID": event_id or None},
        "事实核对": {"草稿": draft, "修订": revision, "判定": [item["grade"] for item in findings]},
        "附件检查": {"可发送": photo_accepted, "原因": reason},
        "发送恢复": {key: segment["status"] for key, segment in delivery["segments"].items()},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
