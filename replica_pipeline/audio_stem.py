"""Source audio stem separation: split original video into vocals + clean BGM.

Supports the core remake audio workflow:
1. Separate original audio into spoken vocals (`qa/source-vocals.wav`) and
   accompaniment (`qa/bgm.wav`) using Demucs (recommended), `audio-separator`,
   or optional stereo `center-cancel` fallback.
2. Evaluate residual accompaniment energy (`evaluate_bgm_track`) so dry-speech
   sources without real music do NOT produce fake ambient-noise `qa/bgm.wav`.
3. Persist provenance & `source_sha256` in `qa/STEM-SPLIT.json` for idempotent
   caching and fail-closed downstream checks.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from .video_qa import audio_db_bins, file_sha256, find_active_segments
except ImportError:
    from video_qa import audio_db_bins, file_sha256, find_active_segments

STEM_MANIFEST_NAME = "qa/STEM-SPLIT.json"
VOCALS_REL = "qa/source-vocals.wav"
BGM_REL = "qa/bgm.wav"
VALID_ENGINES = ("auto", "demucs", "audio-separator", "center-cancel")


def probe_audio_stream(media_path: Path) -> dict[str, Any]:
    """Return first audio stream properties or {'has_audio': False}."""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels,sample_rate:format=duration",
            "-of",
            "json",
            str(media_path),
        ],
        text=True,
    )
    info = json.loads(out)
    streams = info.get("streams") or []
    if not streams:
        return {"has_audio": False, "channels": 0, "sample_rate": 0, "duration": 0.0}
    st = streams[0]
    dur = float((info.get("format") or {}).get("duration") or 0.0)
    return {
        "has_audio": True,
        "channels": int(st.get("channels") or 0),
        "sample_rate": int(st.get("sample_rate") or 0),
        "duration": round(dur, 3),
    }


def _demucs_command() -> list[str] | None:
    if shutil.which("demucs"):
        return ["demucs"]
    uvx = shutil.which("uvx")
    if uvx:
        try:
            subprocess.run(
                [uvx, "--offline", "--from", "demucs", "demucs", "--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )
            return [uvx, "--offline", "--from", "demucs", "demucs"]
        except Exception:
            return [uvx, "--from", "demucs", "demucs"]
    return None


def resolve_stem_engine(engine: str = "auto") -> tuple[str, list[str] | None]:
    """Resolve requested stem separation backend to (engine_name, cmd_prefix)."""
    eng = (engine or "auto").strip().lower()
    if eng not in VALID_ENGINES:
        raise ValueError(f"Unsupported engine {engine!r}; choose from {VALID_ENGINES}")

    if eng == "center-cancel":
        return "center-cancel", None
    if eng == "audio-separator":
        exe = shutil.which("audio-separator")
        if not exe:
            raise RuntimeError("audio-separator not found on PATH")
        return "audio-separator", [exe]
    if eng == "demucs":
        cmd = _demucs_command()
        if not cmd:
            raise RuntimeError("Neither demucs nor uvx was found on PATH")
        return "demucs", cmd

    # auto mode prefers real ML stem separation, never silently degrades to center-cancel
    if shutil.which("demucs"):
        return "demucs", ["demucs"]
    if shutil.which("audio-separator"):
        return "audio-separator", [shutil.which("audio-separator") or "audio-separator"]
    if shutil.which("uvx"):
        cmd = _demucs_command()
        if cmd:
            return "demucs", cmd
    raise RuntimeError(
        "No stem separator found (demucs / audio-separator / uvx). "
        "Install demucs (or uv), or pass --engine center-cancel for stereo fallback."
    )


def evaluate_bgm_track(
    bgm_wav: Path,
    *,
    thresh_db: float = -42.0,
    min_active_ratio: float = 0.20,
    min_p90_db: float = -38.0,
) -> dict[str, Any]:
    """Measure accompaniment energy to verify source actually contains usable BGM."""
    dbs = audio_db_bins(bgm_wav)
    if not dbs:
        return {
            "usable_bgm": False,
            "duration": 0.0,
            "active_ratio": 0.0,
            "p90_db": -120.0,
            "mean_db": -120.0,
            "reason": "empty residual audio",
        }
    segs = find_active_segments(dbs, thresh_db=thresh_db, merge_gap_sec=0.35)
    active_bins = sum(1 for db in dbs if db > thresh_db)
    active_ratio = active_bins / len(dbs)
    sorted_dbs = sorted(dbs)
    p90_db = sorted_dbs[min(len(sorted_dbs) - 1, int(0.90 * len(sorted_dbs)))]
    mean_db = sum(dbs) / len(dbs)
    usable = active_ratio >= min_active_ratio and p90_db >= min_p90_db
    reason = "" if usable else (
        f"residual music too weak/sparse (active_ratio={active_ratio:.2f}, p90={p90_db:.1f}dBFS) "
        "— source appears to be dry speech without BGM"
    )
    return {
        "usable_bgm": usable,
        "duration": round(len(dbs) * 0.05, 3),
        "segments_count": len(segs),
        "active_ratio": round(active_ratio, 3),
        "p90_db": round(p90_db, 2),
        "mean_db": round(mean_db, 2),
        "reason": reason,
    }


def _extract_source_wav(source: Path, out_wav: Path) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        check=True,
    )


def _normalize_stereo_wav(src_wav: Path, dest_wav: Path) -> None:
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src_wav),
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(dest_wav),
        ],
        check=True,
    )


def _separate_with_demucs(
    cmd_prefix: list[str],
    src_wav: Path,
    work_dir: Path,
    *,
    model: str = "htdemucs",
) -> tuple[Path, Path]:
    sep_out = work_dir / "demucs_out"
    sep_out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*cmd_prefix, "--two-stems=vocals", "-n", model, "-o", str(sep_out), str(src_wav)],
        check=True,
    )
    vocals = next(sep_out.rglob("vocals.wav"), None)
    no_vocals = next(sep_out.rglob("no_vocals.wav"), None)
    if not vocals or not no_vocals:
        raise FileNotFoundError(f"Demucs did not emit vocals.wav + no_vocals.wav under {sep_out}")
    return vocals, no_vocals


def _separate_with_audio_separator(cmd_prefix: list[str], src_wav: Path, work_dir: Path) -> tuple[Path, Path]:
    sep_out = work_dir / "sep_out"
    sep_out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*cmd_prefix, str(src_wav), "--output_dir", str(sep_out), "--output_format", "WAV"],
        check=True,
    )
    wavs = sorted(sep_out.glob("*.wav"))
    vocals = next((w for w in wavs if "vocal" in w.name.lower()), None)
    bgm = next((w for w in wavs if "instr" in w.name.lower() or "no_vocal" in w.name.lower()), None)
    if not vocals or not bgm:
        raise FileNotFoundError(f"audio-separator did not emit vocals + instrumental WAVs in {sep_out}")
    return vocals, bgm


def _separate_with_center_cancel(src_wav: Path, work_dir: Path, *, channels: int) -> tuple[Path, Path]:
    if channels < 2:
        raise RuntimeError(
            "center-cancel separation requires a stereo source (>=2 audio channels); use demucs for mono"
        )
    vocals = work_dir / "center_vocals.wav"
    bgm = work_dir / "side_bgm.wav"
    # Approximate center-channel isolation for speech and mid-subtracted L-R for BGM.
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src_wav),
            "-af",
            "pan=mono|c0=0.5*c0+0.5*c1",
            "-c:a",
            "pcm_s16le",
            str(vocals),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src_wav),
            "-af",
            "pan=stereo|c0=0.707*c0-0.707*c1|c1=0.707*c0-0.707*c1",
            "-c:a",
            "pcm_s16le",
            str(bgm),
        ],
        check=True,
    )
    return vocals, bgm


def extract_source_stems(
    film: Path,
    source: Path,
    *,
    engine: str = "auto",
    model: str = "htdemucs",
    force: bool = False,
) -> dict[str, Any]:
    """Split `source` audio into `qa/source-vocals.wav` + `qa/bgm.wav` and write `qa/STEM-SPLIT.json`."""
    film = Path(film)
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"missing source video: {source}")

    qa_dir = film / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    manifest_p = film / STEM_MANIFEST_NAME
    vocals_dest = film / VOCALS_REL
    bgm_dest = film / BGM_REL
    src_sha = file_sha256(source)

    if not force and manifest_p.is_file():
        try:
            cached = json.loads(manifest_p.read_text())
            same_src = cached.get("source_sha256") == src_sha
            same_eng = engine == "auto" or cached.get("engine") == engine
            vocals_ok = not cached.get("vocals") or (film / cached["vocals"]).is_file()
            bgm_ok = not cached.get("usable_bgm") or bgm_dest.is_file()
            if same_src and same_eng and vocals_ok and bgm_ok:
                return cached
        except Exception:
            pass

    audio_info = probe_audio_stream(source)
    if not audio_info["has_audio"]:
        bgm_dest.unlink(missing_ok=True)
        vocals_dest.unlink(missing_ok=True)
        payload = {
            "version": 1,
            "source": str(source),
            "source_sha256": src_sha,
            "has_audio": False,
            "usable_bgm": False,
            "vocals": None,
            "bgm": None,
            "engine": "none",
            "reason": "source video has no audio track",
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload

    chosen_engine, cmd_prefix = resolve_stem_engine(engine)
    work_dir = qa_dir / ".stem-split-work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    try:
        src_wav = work_dir / "source_raw.wav"
        _extract_source_wav(source, src_wav)
        if chosen_engine == "demucs":
            assert cmd_prefix is not None
            voc_cand, bgm_cand = _separate_with_demucs(cmd_prefix, src_wav, work_dir, model=model)
        elif chosen_engine == "audio-separator":
            assert cmd_prefix is not None
            voc_cand, bgm_cand = _separate_with_audio_separator(cmd_prefix, src_wav, work_dir)
        else:
            voc_cand, bgm_cand = _separate_with_center_cancel(
                src_wav, work_dir, channels=int(audio_info["channels"])
            )

        _normalize_stereo_wav(voc_cand, vocals_dest)
        metrics = evaluate_bgm_track(bgm_cand)
        if metrics["usable_bgm"]:
            _normalize_stereo_wav(bgm_cand, bgm_dest)
            bgm_rel: str | None = BGM_REL
            bgm_sha: str | None = file_sha256(bgm_dest)
        else:
            bgm_dest.unlink(missing_ok=True)
            bgm_rel = None
            bgm_sha = None

        payload = {
            "version": 1,
            "source": str(source),
            "source_sha256": src_sha,
            "has_audio": True,
            "channels": audio_info["channels"],
            "engine": chosen_engine,
            "model": model if chosen_engine == "demucs" else None,
            "vocals": VOCALS_REL,
            "vocals_sha256": file_sha256(vocals_dest),
            "bgm": bgm_rel,
            "bgm_sha256": bgm_sha,
            "usable_bgm": bool(metrics["usable_bgm"]),
            "bgm_metrics": metrics,
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
