"""Settings come from environment variables, then .env, then the defaults below. A variable that is set but empty
counts as not set. A value that cannot be read stops the start, with a message that names the setting."""
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path


class SettingError(ValueError):
    """A setting whose value cannot be read; the message names it and says what to write instead."""


@dataclass
class Settings:
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_concurrency: int = 6
    llm_thinking: bool = False
    host: str = "0.0.0.0"
    port: int = 8620
    allowed_clients: str = ""  # comma-separated addresses or ranges that may connect; empty = anyone who can reach the port
    data_dir: Path = Path("data")
    max_upload_mb: int = 200
    delete_after_download_min: int = 30
    job_ttl_hours: int = 24
    job_timeout_min: int = 180  # a job still running after this long is stopped; 0 = no limit
    face_default_on: bool = True
    face_score: float = 0.7
    face_model: Path = Path("models/yunet.onnx")
    mask_full: str = "■"
    mask_half: str = "*"
    ocr_workers: int = 4
    # Quality comes before speed here. These limits only stop a model that keeps going in circles.
    agent_max_steps: int = 60
    agent_extra_steps: int = 20  # steps the agent may still take once every unit has been checked
    use_agent: bool = True
    chunk_chars: int = 2500
    recheck_chunk_chars: int = 1200  # a second detection pass over the same text in smaller pieces; 0 = off
    # The code function: after detection, a model decides which names are one person, so they share a number.
    link_names: bool = True
    link_max_steps: int = 30
    pdf_dpi: int = 200
    min_vision_px: int = 48
    sandbox_image: str = "deid-sandbox:1"


SWITCH = {"1": True, "true": True, "yes": True, "on": True, "0": False, "false": False, "no": False, "off": False}
COMMENT = re.compile(r"\s#")  # .env takes a comment on a line of its own; after a value it would be read as part of it
FROM_ENVIRONMENT = "環境變數"


def _convert(name: str, kind, raw: str, where: str):
    if kind is bool:
        # Only the spellings read before: anything else used to be read as off without a word ("ture").
        value = SWITCH.get(raw.strip().lower())
        if value is None:
            raise SettingError(f"設定 {name}（{where}）的值「{raw}」看不懂：開關請寫 true 或 false（也可以寫 1／0、yes／no、on／off）")
        return value
    try:
        return kind(raw)
    except ValueError:
        raise SettingError(f"設定 {name}（{where}）的值「{raw}」不是{'整數' if kind is int else '數字'}") from None


def load_settings(env_file: Path | None = Path(".env"), environ: dict | None = None) -> Settings:
    values: dict[str, tuple[str, str]] = {}  # name -> (value, where it was written)
    if env_file is not None and Path(env_file).is_file():
        for number, line in enumerate(Path(env_file).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = (val.strip(), f"{Path(env_file).name} 第 {number} 行")
    for key, val in (os.environ if environ is None else environ).items():
        # Set but empty is not set, for every setting: an empty value already meant "use the default", and it must
        # not hide what .env says either. An empty ALLOWED_CLIENTS in the environment erased the list in .env and
        # opened the port to anyone who can reach it.
        if val.strip():
            values[key] = (val, FROM_ENVIRONMENT)
    s = Settings()
    problems = []  # all of them at once: whoever starts the service fixes them in one go
    for f in fields(Settings):
        name = f.name.upper()
        raw, where = values.get(name, ("", ""))
        if not raw:
            continue
        try:
            if where != FROM_ENVIRONMENT and COMMENT.search(raw):
                # The value is not repeated here: it may be the model endpoint's key.
                raise SettingError(f"設定 {name}（{where}）的值後面接著註解。註解要寫在單獨一行，以 # 開頭")
            setattr(s, f.name, _convert(name, type(getattr(s, f.name)), raw, where))
        except SettingError as e:
            problems.append(str(e))
    if problems:
        raise SettingError("\n".join(problems))
    return s
