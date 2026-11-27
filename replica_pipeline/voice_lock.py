"""Film-level voice lock: extract S01 spoken audio and reuse on later shots."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


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
    if wav.is_file() and not force:
        return json.loads(voice_meta_path(film).read_text()) if voice_meta_path(film).is_file() else {
            "wav": str(wav.relative_to(film)),
            "source_shot": shot,
            "skipped": True,
        }

    # Prefer mid-speech: skip tiny head silence, take up to max_sec
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
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
    voice_meta_path(film).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )
    return meta


def ensure_voice_lock(film: Path, *, force: bool = False) -> dict:
    film = Path(film)
    meta_p = voice_meta_path(film)
    wav = voice_lock_path(film)
    if wav.is_file() and meta_p.is_file() and not force:
        return json.loads(meta_p.read_text())
    return extract_voice_from_shot(film, force=force)
