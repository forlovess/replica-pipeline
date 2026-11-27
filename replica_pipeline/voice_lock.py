"""Film-level voice lock: extract S01 spoken audio and reuse on later shots."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

try:
    from .video_qa import scan_audio_glitches
except ImportError:
    from video_qa import scan_audio_glitches


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def first_shot_id(film: Path) -> str:
    plan = film / "qa" / "REBEAT-PLAN.json"
    if plan.is_file():
        shots = json.loads(plan.read_text()).get("shots") or []
        if shots:
            return str(shots[0]["id"])
    return "S01"


def voice_lock_dir(film: Path) -> Path:
    return film / "qa" / "voice-lock"


def voice_lock_path(film: Path) -> Path:
    return voice_lock_dir(film) / "S01-voice.wav"


def voice_meta_path(film: Path) -> Path:
    return film / "qa" / "VOICE-LOCK.json"


def extract_voice_from_shot(
    film: Path,
    shot: str | None = None,
    *,
    max_sec: float = 12.0,
    force: bool = False,
) -> dict:
    """Pull mono 16k WAV from the first shot for film-wide timbre lock."""
    film = Path(film)
    shot = shot or first_shot_id(film)
    src = film / "shots" / f"{shot}.mp4"
    if not src.is_file():
        raise FileNotFoundError(f"missing {src} — generate {shot} before voice lock")

    out_dir = voice_lock_dir(film)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = voice_lock_path(film)
    src_sha = _file_sha256(src)
    meta_p = voice_meta_path(film)
    if wav.is_file() and meta_p.is_file() and not force:
        existing = json.loads(meta_p.read_text())
        if existing.get("source_sha256") == src_sha:
            return existing

    # Skip leading silence or isolated glitch burst so S01-voice.wav holds clean speech.
    trim_start = 0.0
    try:
        glitch_info = scan_audio_glitches(src)
        trim_start = float(glitch_info["recommended_trim"][0])
    except Exception:
        trim_start = 0.0

    cmd = ["ffmpeg", "-y", "-v", "error"]
    if trim_start > 0.05:
        cmd.extend(["-ss", f"{trim_start:.3f}"])
    cmd.extend(
        [
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-t",
            str(max_sec),
            str(wav),
        ]
    )
    subprocess.run(cmd, check=True)

    # Duration probe
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(wav),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    dur = float(probe.stdout.strip() or 0)

    meta = {
        "source_shot": shot,
        "source_sha256": src_sha,
        "trim_start_sec": trim_start,
        "wav": str(wav.relative_to(film)),
        "sample_rate": 16000,
        "channels": 1,
        "duration_sec": dur,
        "max_sec": max_sec,
        "prompt_tag": (
            "<Audio 1> is the voice-timbre lock for (S1) across the whole film; "
            "keep the same speaker identity and room tone."
        ),
        "rule": "S02+ must submit ref2v with referenceAudiosBase64=[this wav] + still as referenceImagesBase64",
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def ensure_voice_lock(film: Path, *, force: bool = False) -> dict:
    film = Path(film)
    return extract_voice_from_shot(film, force=force)
