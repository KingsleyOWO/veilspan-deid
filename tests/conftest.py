import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deid.config import load_settings

ROOT = Path(__file__).resolve().parent.parent


def _live_settings():
    return load_settings(ROOT / ".env")


def pytest_collection_modifyitems(config, items):
    live_ok = bool(_live_settings().llm_api_key) and os.environ.get("DEID_SKIP_LIVE") != "1"
    try:
        import rapidocr  # noqa: F401
        ocr_ok = True
    except Exception:
        ocr_ok = False
    docker_ok = False
    if shutil.which("docker"):
        docker_ok = subprocess.run(["docker", "image", "inspect", _live_settings().sandbox_image],
                                   capture_output=True).returncode == 0
    for item in items:
        if "live" in item.keywords and not live_ok:
            item.add_marker(pytest.mark.skip(reason="LLM_API_KEY not configured"))
        if "ocr" in item.keywords and not ocr_ok:
            item.add_marker(pytest.mark.skip(reason="rapidocr not installed"))
        if "docker" in item.keywords and not docker_ok:
            item.add_marker(pytest.mark.skip(reason="sandbox image not built"))


@pytest.fixture
def settings(tmp_path):
    """Defaults only: never reads the developer's .env, never points at a real endpoint. The model step added
    after detection (same-person names) is off: the doubles most tests use answer detection requests only.
    Tests about that step switch it on."""
    s = load_settings(tmp_path / "none.env", environ={})
    s.link_names = False
    s.data_dir = tmp_path / "data"
    s.llm_base_url, s.llm_model, s.llm_api_key = "http://127.0.0.1:9/v1", "test-model", "test-key"
    return s


@pytest.fixture
def live_settings():
    return _live_settings()
