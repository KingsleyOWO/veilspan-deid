from pathlib import Path

from deid.config import Settings, load_settings
from deid.model import (Finding, ImageUnit, Inventory, MaskPlan, MetaUnit, Occurrence, OcrLine, TextUnit,
                        Unchecked, finding_from_json, inventory_from_json, to_json)


def sample_inventory():
    line = OcrLine(text="王小明", box=(1.0, 2.0, 31.0, 12.0), chars=[(1, 2, 11, 12), (11, 2, 21, 12), (21, 2, 31, 12)])
    return Inventory(
        kind="docx",
        texts=[TextUnit(id="t1", text="承辦人 | 王小明", section="內文"), TextUnit(id="t2", text="x", section="頁首", mirror=True)],
        images=[ImageUnit(id="img1", name="word/media/image1.png", width=100, height=50, supported=True, anchor="t1",
                          areas=[(0, 0, 100, 50)], ocr=[line], faces=[(5, 5, 20, 20)])],
        metas=[MetaUnit(id="m1", label="作者", value="林志豪", person=True)],
        unchecked=[Unchecked(name="word/embeddings/oleObject1.bin", reason="內嵌物件")],
    )


def test_inventory_survives_json_roundtrip():
    inv = sample_inventory()
    again = inventory_from_json(to_json(inv))
    assert again == inv
    assert isinstance(again.images[0].ocr[0].chars[0], tuple)
    assert again.images[0].needs_scan is True


def test_finding_survives_json_roundtrip():
    f = Finding(id="f1", kind="text", type="姓名", text="王小明", source="model", on=True, reason="r", occurrences=[
        Occurrence(unit="t1", kind="text", start=6, end=9),
        Occurrence(unit="img1", kind="box", box=(1, 2, 31, 12), fuzzy=True),
        Occurrence(unit="t9", kind="text", start=0, end=3, page_boxes=[("pg1", (1, 2, 3, 4))], on=False),
    ])
    again = finding_from_json(to_json(f))
    assert again == f
    assert again.occurrences[2].page_boxes == [("pg1", (1, 2, 3, 4))]


def test_mask_plan_defaults_are_independent():
    a, b = MaskPlan(), MaskPlan()
    a.text["t1"] = [(0, 1)]
    assert b.text == {}


def test_settings_defaults_match_the_plan(tmp_path):
    s = load_settings(tmp_path / "missing.env", environ={})
    assert (s.port, s.host, s.llm_concurrency, s.ocr_workers, s.agent_max_steps) == (8620, "0.0.0.0", 6, 4, 60)
    assert (s.agent_extra_steps, s.use_agent) == (20, True)
    assert (s.chunk_chars, s.pdf_dpi, s.face_score, s.face_default_on, s.min_vision_px) == (2500, 200, 0.7, True, 48)
    assert (s.max_upload_mb, s.delete_after_download_min, s.job_ttl_hours) == (200, 30, 24)
    assert (s.mask_full, s.mask_half, s.sandbox_image, s.llm_thinking) == ("■", "*", "deid-sandbox:1", False)
    assert s.data_dir == Path("data")


def test_settings_reads_env_file_and_environment_wins(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nLLM_BASE_URL=http://file/v1\nLLM_API_KEY = filekey \nPORT=9000\nFACE_DEFAULT_ON=false\n", encoding="utf-8")
    s = load_settings(env, environ={"PORT": "9100", "LLM_MODEL": "m"})
    assert s.llm_base_url == "http://file/v1"
    assert s.llm_api_key == "filekey"
    assert s.port == 9100
    assert s.llm_model == "m"
    assert s.face_default_on is False
    assert isinstance(s, Settings)
