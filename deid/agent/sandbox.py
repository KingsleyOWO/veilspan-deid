"""Runs a script the model wrote, inside a locked-down container: no network, read-only file system,
not root, bounded memory/CPU/time. The script can only read the uploaded file and print text."""
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from deid.config import Settings

OUTPUT_LIMIT = 8000  # characters handed back to the model
CAPTURE_LIMIT = 4 * OUTPUT_LIMIT  # bytes kept while the script runs; the rest is read and thrown away
_checked: dict[str, tuple[float, bool]] = {}


def available(settings: Settings) -> bool:
    image = settings.sandbox_image
    now = time.time()
    if image in _checked and now - _checked[image][0] < 60:
        return _checked[image][1]
    ok = False
    if image and shutil.which("docker"):
        try:
            ok = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _checked[image] = (now, ok)
    return ok


def _copy_folder(settings: Settings, src: Path, workdir: Path | None) -> Path | None:
    """Where the copy of the upload is made (None: the system's temporary folder). Docker mounts the copy from the
    host, so under the service container it has to be a place the host sees under the same path. That is the data
    folder; the container's own /tmp is not. An upload inside the data folder (the service) gets its copy beside it,
    in the job's folder, which goes with the job. A file anywhere else (command line use, no container) gets the
    system's temporary folder rather than a new folder beside the user's own document."""
    if workdir is not None:
        return workdir
    return src.parent if src.is_relative_to(Path(settings.data_dir).resolve()) else None


def run_python(settings: Settings, src: Path, code: str, timeout: int = 60, *, workdir: Path | None = None) -> dict:
    """The upload is stored readable by the service user only (mode 0600) and the container runs as another
    user (65534), so the container is not given the upload itself but a copy of it. The copy sits in a folder
    that only the service user can enter, so on the host it stays private; the container sees nothing but that
    one file, read-only, and the copy is removed when the run ends, however it ends."""
    name = f"deid-sbx-{secrets.token_hex(6)}"
    src = Path(src).resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="sandbox-", dir=_copy_folder(settings, src, workdir)) as folder:
            sandbox_input = Path(folder).resolve() / f"input{src.suffix.lower()}"
            shutil.copyfile(src, sandbox_input)
            sandbox_input.chmod(0o444)
            cmd = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only",
                   "--tmpfs", "/tmp:size=64m", "--user", "65534:65534", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--memory", "1g", "--memory-swap", "1g", "--cpus", "1",
                   "--pids-limit", "128", "-v", f"{sandbox_input}:/work/{sandbox_input.name}:ro", settings.sandbox_image,
                   "python", "-I", "-"]
            res = run_bounded(cmd, code.encode("utf-8"), timeout,
                              on_timeout=lambda: subprocess.run(["docker", "kill", name], capture_output=True))
    except OSError:
        return {"ok": False, "timed_out": False, "output": "", "error": "腳本環境無法啟動"}
    text = res.data.decode("utf-8", "replace")
    if res.timed_out:
        return {"ok": False, "timed_out": True, "output": text[:OUTPUT_LIMIT]}
    out = {"ok": res.returncode == 0, "timed_out": False, "output": text[:OUTPUT_LIMIT]}
    if res.truncated or len(text) > OUTPUT_LIMIT:
        out["truncated"] = True
    return out


@dataclass
class Bounded:
    returncode: int | None
    data: bytes  # standard output and error together, in the order they were written
    truncated: bool
    timed_out: bool


def run_bounded(cmd: list[str], stdin_data: bytes, timeout: float, on_timeout=None) -> Bounded:
    """Run a command, keeping at most CAPTURE_LIMIT bytes of what it prints. Output is read as it comes
    and the excess dropped, so a script that prints without end cannot fill this process's memory."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    kept = bytearray()
    state = {"truncated": False}

    def drain():
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            room = CAPTURE_LIMIT - len(kept)
            if room > 0:
                kept.extend(chunk[:room])
            if len(chunk) > room:
                state["truncated"] = True

    def feed():
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except OSError:
            pass  # the command ended without reading its input

    threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=feed, daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout:
            on_timeout()
        proc.kill()
        proc.wait()
    for t in threads:
        t.join(timeout=5)
    proc.stdout.close()
    return Bounded(proc.returncode, bytes(kept), state["truncated"], timed_out)
