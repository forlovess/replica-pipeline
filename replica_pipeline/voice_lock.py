"""Film-level voice lock: extract S01 spoken audio and reuse on later shots."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

try:
    from .video_qa import scan_audio_glitches
    from .script_lint import SHOT_RE, compile_shot_text, read_strict_json
except ImportError:
    from video_qa import scan_audio_glitches
    from script_lint import SHOT_RE, compile_shot_text, read_strict_json


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


def shot_roles(film: Path, *, prompt_overrides: dict[str, str] | None = None) -> dict[str, str]:
    plan = film / "qa/REBEAT-PLAN.json"
    rows = (read_strict_json(plan).get("shots") or []) if plan.is_file() else [{"id": "S01"}]
    if not plan.is_file():
        rows.extend({"id": sid} for sid in (prompt_overrides or {}) if sid != "S01" and SHOT_RE.fullmatch(sid))
    roles = {}
    for row in rows:
        if row.get("silent") or row.get("audio_owner", row["id"]) != row["id"]:
            roles[row["id"]] = ""
            continue
        prompt = film / "prompts" / f"{row['id']}.txt"
        prompt_text = (prompt_overrides or {}).get(row["id"])
        if prompt_text is None and prompt.is_file():
            prompt_text = prompt.read_text()
        projected = compile_shot_text(prompt_text, default_role=row.get("role", "HOST")) if prompt_text else None
        raw = row.get("spoken") or row.get("vo") or ""
        role = compile_shot_text(raw, default_role=row.get("role", projected["role"] if projected else "HOST"))["role"]
        if row.get("role", role) != role or (projected and projected["role"] != role and "<" + projected["role"] + ">" in prompt_text):
            raise ValueError(f"{row['id']}: conflicting role cues")
        roles[row["id"]] = role
    return roles


def voice_requirements(film: Path, shot: str, *, strict: bool = True, prompt: str | None = None) -> tuple[str, str, Path | None]:
    roles = shot_roles(film, prompt_overrides={shot: prompt} if prompt is not None else None)
    if shot not in roles:
        raise ValueError(f"{shot}: not in REBEAT-PLAN")
    role = roles[shot]
    if not role:
        return role, shot, None
    bootstrap = next(sid for sid, value in roles.items() if value == role)
    meta_path = voice_meta_path(film)
    meta = read_strict_json(meta_path) if meta_path.is_file() else {}
    entry = (meta.get("roles") or {}).get(role)
    if entry is None and role == "HOST" and not meta.get("roles") and meta:
        entry = meta
    if not entry:
        if not strict and role == "HOST" and not meta and shot != bootstrap and voice_lock_path(film).is_file():
            return role, bootstrap, voice_lock_path(film)
        if shot != bootstrap:
            raise ValueError(f"{shot}: missing {role} voice bank; accept {bootstrap} first")
        return role, bootstrap, None
    wav = film / (entry.get("wav") or "qa/voice-lock/S01-voice.wav")
    source = film / "shots" / f"{bootstrap}.mp4"
    if (not wav.is_file() or not source.is_file() or entry.get("source_shot", bootstrap) != bootstrap
            or entry.get("source_sha256") != _file_sha256(source)
            or (entry.get("wav_sha256") or strict) and entry.get("wav_sha256") != _file_sha256(wav)):
        if shot == bootstrap:
            return role, bootstrap, None
        raise ValueError(f"{shot}: stale {role} voice bank; re-accept {bootstrap}")
    return role, bootstrap, wav


def extract_voice_from_shot(
    film: Path,
    shot: str | None = None,
    *,
    max_sec: float = 12.0,
    force: bool = False,
    role: str | None = None,
) -> dict:
    """Pull mono 16k WAV from the first shot for film-wide timbre lock."""
    film = Path(film)
    roles = shot_roles(film)
    shot = shot or (next((sid for sid, value in roles.items() if value == role), None) if role else first_shot_id(film))
    if not shot or not SHOT_RE.fullmatch(shot) or not roles.get(shot) or (role is not None and roles[shot] != role):
        raise ValueError("shot and role must identify a voiced shot in the plan")
    role = roles[shot]
    if shot != next(sid for sid, value in roles.items() if value == role):
        raise ValueError(f"{shot}: voice must be extracted from the {role} bootstrap shot")
    src = film / "shots" / f"{shot}.mp4"
    if not src.is_file():
        raise FileNotFoundError(f"missing {src} — generate {shot} before voice lock")

    out_dir = voice_lock_dir(film)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = voice_lock_path(film) if role == "HOST" else out_dir / f"{role}-voice.wav"
    src_sha = _file_sha256(src)
    meta_p = voice_meta_path(film)
    existing = read_strict_json(meta_p) if meta_p.is_file() else {}
    banks = dict(existing.get("roles") or {})
    if not banks and existing.get("source_shot") and existing.get("wav") and voice_lock_path(film).is_file():
        banks["HOST"] = {**existing, "wav_sha256": _file_sha256(voice_lock_path(film))}
    bank = banks.get(role, {})
    if wav.is_file() and not force and bank.get("source_sha256") == src_sha and bank.get("wav_sha256") == _file_sha256(wav):
        return {**bank, "roles": banks}

    # Skip leading silence or isolated glitch burst so S01-voice.wav holds clean speech.
    trim_start = 0.0
    try:
        glitch_info = scan_audio_glitches(src)
        trim_start = float(glitch_info["recommended_trim"][0])
    except Exception:
        trim_start = 0.0

    temporary = wav.with_suffix(".tmp.wav")
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
            str(temporary),
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
            str(temporary),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    dur = float(probe.stdout.strip() or 0)
    if dur <= 0:
        raise ValueError(f"{shot}: empty voice reference")
    temporary.replace(wav)

    meta = {
        "source_shot": shot,
        "source_sha256": src_sha,
        "trim_start_sec": trim_start,
        "wav": str(wav.relative_to(film)),
        "wav_sha256": _file_sha256(wav),
        "sample_rate": 16000,
        "channels": 1,
        "duration_sec": dur,
        "max_sec": max_sec,
        "prompt_tag": (
            f"<Audio 1> is the voice-timbre lock for {role} across the film; "
            "keep the same speaker identity and room tone."
        ),
        "rule": "Subsequent shots of this role use ref2v with its WAV and the shot's still",
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    banks[role] = meta
    temporary_meta = meta_p.with_suffix(".tmp")
    temporary_meta.write_text(json.dumps({**banks.get("HOST", {}), "roles": banks}, ensure_ascii=False, indent=2) + "\n")
    temporary_meta.replace(meta_p)
    return {**meta, "roles": banks}


def ensure_voice_lock(film: Path, *, force: bool = False, role: str | None = None, shot: str | None = None) -> dict:
    film = Path(film)
    return extract_voice_from_shot(film, shot, force=force, role=role)
