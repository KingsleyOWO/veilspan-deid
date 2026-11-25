"""The detection prompts and the detection agent's tools are what was measured on real documents. New model
steps bring their own prompts; a change to these would need the measurements done again.
Fingerprints taken on 2026-11-18 at commit 581591d."""
import hashlib
import json

from deid import detect
from deid.agent import loop, tools


def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_detection_prompts_and_tools_are_what_was_measured():
    assert h(loop.SYSTEM) == "ba0195df69b62f637155083d00634082ca02fa63d8292b6c24e2a30250603323"
    assert h(detect.SYSTEM_PROMPT) == "d99b25ee6deba8d232344cc70a2d7a17869f5fd04f7d0d45e6145bee22b3f54b"
    assert h(json.dumps(tools.SCHEMAS, ensure_ascii=False, sort_keys=True)) == \
        "897bd87229039ea4859e1bd66ac6fd42f1541e4ddb12011e36ddcd0950cddb59"
