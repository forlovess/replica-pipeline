"""Resumable FilmDesk upscaler client and stage-seven evidence."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

try:
    from .gate import require
    from .script_lint import read_strict_json
    from .video_qa import file_sha256
except ImportError:
    from gate import require
    from script_lint import read_strict_json
    from video_qa import file_sha256

CHUNK_BYTES = 32 * 1024 * 1024
USER_AGENT = "FilmDesk-h3-superres-api/1.0 (+https://eclatly.tech)"


def _api_key() -> str:
    for key in ("SUPERRES_API_KEY", "H3_SUPERRES_API_KEY"):
        if value := os.environ.get(key, "").strip():
            return value
    if security := shutil.which("security"):
        for service in ("video_remix.api", "FilmDesk-h3-superres-api"):
            for account in ("SUPERRES_API_KEY", "H3_SUPERRES_API_KEY"):
                result = subprocess.run([security, "find-generic-password", "-s", service, "-a", account, "-w"], capture_output=True, text=True)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
    raise ValueError("set SUPERRES_API_KEY or H3_SUPERRES_API_KEY")


def _request(base: str, key: str, path: str, *, method: str = "GET", payload=None, content_type: str | None = None):
    headers = {"User-Agent": USER_AGENT}
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
        content_type = content_type or "application/json"
    elif isinstance(payload, bytes) and content_type is None:
        content_type = "application/octet-stream"
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
    request.add_unredirected_header("X-API-Key", key)
    return urllib.request.urlopen(request, timeout=300)


def _json_request(base, key, path, **kwargs) -> dict:
    with _request(base, key, path, **kwargs) as response:
        raw = response.read()
    if not raw:
        return {}
    envelope = json.loads(raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data", envelope), dict):
        raise ValueError("superres server returned a malformed response")
    return envelope.get("data", envelope)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _probe(path: Path) -> dict:
    output = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height:format=format_name,duration", "-of", "json", str(path)], text=True)
    data = json.loads(output)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError(f"{path.name}: no video stream")
    return {"width": int(streams[0]["width"]), "height": int(streams[0]["height"]),
            "format": data["format"]["format_name"], "duration": float(data["format"]["duration"])}


def _audio_count(path: Path) -> int:
    try:
        output = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0
    return sum(bool(line.strip()) for line in output.splitlines())


def _preserve_source_audio(source: Path, output: Path) -> bool:
    """Copy the accepted mix back when the upscaler returns a silent picture."""
    if _audio_count(source) < 1 or _audio_count(output) > 0:
        return False
    muxed = output.with_name(output.name + ".audio.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(output), "-i", str(source),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
             "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(muxed)],
            check=True,
        )
        muxed.replace(output)
        return True
    finally:
        muxed.unlink(missing_ok=True)


def _verify_video(source: Path, output: Path) -> dict:
    original, upscaled = _probe(source), _probe(output)
    width, height = upscaled["width"], upscaled["height"]
    if ("mp4" not in upscaled["format"].split(",") or width * 16 != height * 9 or width * height <= 720 * 1280
            or width != original["width"] * 2 or height != original["height"] * 2
            or not math.isfinite(upscaled["duration"]) or upscaled["duration"] <= 0
            or abs(upscaled["duration"] - original["duration"]) > 0.5):
        raise ValueError("superres output must be a 2x 9:16 MP4 above 720×1280 with matching duration")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], check=True)
    return upscaled


def validate_superres_data(film: Path) -> list[str]:
    film = Path(film)
    try:
        evidence = read_strict_json(film / "qa/SUPERRES.json")
        source, final = film / "qa/captioned.mp4", film / f"{film.name}.mp4"
        if (evidence["status"] != "PASS" or evidence["scale"] != 2 or evidence["input_sha256"] != file_sha256(source)
                or evidence["output_sha256"] != file_sha256(final) or evidence["output_bytes"] != final.stat().st_size):
            raise ValueError("SUPERRES.json: stale or incomplete output evidence")
        _verify_video(source, final)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return [str(exc)]
    return []


def run_superres(film: Path, *, base_url: str | None = None, resume: bool = False,
                 poll_interval: float = 5.0, timeout: float = 3600.0) -> dict:
    film = Path(film).resolve()
    require(film, "superres")
    source, final = film / "qa/captioned.mp4", film / f"{film.name}.mp4"
    task_path, lock_path = film / "qa/superres.task.json", film / "qa/SUPERRES.json"
    base = (base_url or os.environ.get("SUPERRES_BASE_URL") or os.environ.get("H3_SUPERRES_URL") or "").rstrip("/")
    with (film / "qa/.superres.lock").open("a") as mutex:
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("superres is already running for this film") from exc
        digest = file_sha256(source)
        size = source.stat().st_size
        if size <= 0:
            raise ValueError("empty captioned MP4")
        if lock_path.is_file() and not validate_superres_data(film):
            return read_strict_json(lock_path)
        journal = read_strict_json(task_path) if task_path.is_file() else {}
        tasks = journal.get("tasks", {})
        task = tasks.get(digest)
        if task and resume and not base:
            base = task["base_url"]
        url = urlsplit(base)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("set a valid SUPERRES_BASE_URL or H3_SUPERRES_URL")

        def save():
            tasks[digest] = task
            _write_json(task_path, {**task, "tasks": tasks})

        key = _api_key()
        if task:
            if base != task["base_url"]:
                raise ValueError("superres task already exists on a different service; use its original URL")
            if not task.get("task_id"):
                raise ValueError("superres task creation is uncertain; recover task_id before retrying")
        else:
            task = {"input_sha256": digest, "input_bytes": size, "base_url": base, "state": "CREATING"}
            save()
            response = _json_request(base, key, "/v1/upscales", method="POST", payload={"filename": source.name, "scale": 2})
            try:
                task_id = str(UUID(response["taskId"]))
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise ValueError("superres creation response lacked a valid taskId; do not repeat POST") from exc
            task.update(task_id=task_id, state="UPLOADING")
            save()
        endpoint = "/v1/upscales/" + str(UUID(task["task_id"]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remote = _json_request(base, key, endpoint)
            status = remote.get("status")
            task["state"] = status
            save()
            if status == "COMPLETED":
                break
            if status == "UPLOADING":
                offset = remote.get("uploadedBytes", 0)
                if type(offset) is not int or not 0 <= offset <= size:
                    raise ValueError("invalid superres uploadedBytes")
                with source.open("rb") as handle:
                    handle.seek(offset)
                    while offset < size:
                        block = handle.read(min(CHUNK_BYTES, size - offset))
                        if not block:
                            raise ValueError("captioned MP4 changed during upload")
                        committed = _json_request(base, key, f"{endpoint}/input?offset={offset}", method="PUT", payload=block)
                        if committed.get("uploadedBytes") != offset + len(block):
                            raise ValueError("superres server committed an unexpected upload offset")
                        offset += len(block)
                        task["uploadedBytes"] = offset
                        save()
                if file_sha256(source) != digest:
                    raise ValueError("captioned MP4 changed during upload")
                _json_request(base, key, f"{endpoint}/start", method="POST", payload=b"", content_type="application/json")
            elif status != "RUNNING":
                raise ValueError(f"superres task {task['task_id']}: {status or 'missing status'}")
            time.sleep(poll_interval)
        else:
            raise TimeoutError(f"superres task {task['task_id']} still pending; rerun with --resume")
        output = remote.get("output") or {}
        output_hash, output_bytes = output.get("sha256"), output.get("bytes")
        if not isinstance(output_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", output_hash) or type(output_bytes) is not int or output_bytes <= 0:
            raise ValueError("superres server omitted expected output SHA-256 or byte length")
        part = final.with_suffix(".part.mp4")
        actual, downloaded = hashlib.sha256(), 0
        try:
            with _request(base, key, f"{endpoint}/file") as response, part.open("wb") as handle:
                while block := response.read(1024 * 1024):
                    downloaded += len(block)
                    if downloaded > output_bytes:
                        raise ValueError("superres download exceeds advertised byte length")
                    actual.update(block)
                    handle.write(block)
            if downloaded != output_bytes or actual.hexdigest() != output_hash.lower():
                raise ValueError("superres download byte length or SHA-256 mismatch")
            remuxed = _preserve_source_audio(source, part)
            video = _verify_video(source, part)
            if file_sha256(source) != digest:
                raise ValueError("captioned MP4 changed during upscale")
            part.replace(final)
        finally:
            part.unlink(missing_ok=True)
        evidence = {"status": "PASS", "task_id": task["task_id"], "scale": 2, "input_sha256": digest,
                    "download_sha256": actual.hexdigest(), "download_bytes": downloaded,
                    "audio_remuxed": remuxed, "output_sha256": file_sha256(final),
                    "output_bytes": final.stat().st_size, "output": final.name, **video}
        _write_json(lock_path, evidence)
        return evidence
