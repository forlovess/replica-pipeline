"""Post-H3 video visual QA, contact-sheet extraction, and audio glitch detection.

Stops H3 drift from slipping past ASR-only meaning checks:
- Extracts multi-timestamp contact sheets (10%, 35%, 65%, 90%) per shot.
- Scans 50ms PCM audio energy to flag leading isolated glitches (e.g. stray
  syllables followed by silence) and trailing silence.
- Validates structured qa/H3-VISUAL-LOCK.json bound by shot_sha256 so re-rendered
  videos cannot reuse stale PASS records.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

VISUAL_LOCK_NAME = "qa/H3-VISUAL-LOCK.json"
CONTACT_DIR_NAME = "qa/h3-contact-sheets"
VALID_SIZE_VS_SPEC = {"match"}
FRAME_FRACTIONS = (0.10, 0.35, 0.65, 0.90)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


def audio_db_bins(path: Path, *, bin_ms: int = 50) -> list[float]:
    """Return mono 16kHz RMS dBFS per bin_ms window for a video or audio file."""
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-",
        ]
    )
    win = max(160, int(16000 * bin_ms / 1000))
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[: n * 2])
    dbs: list[float] = []
    for i in range(0, n - win + 1, win):
        chunk = samples[i : i + win]
        rms = math.sqrt(sum(x * x for x in chunk) / win) / 32768.0
        dbs.append(20.0 * math.log10(max(rms, 1e-6)))
    return dbs


def find_active_segments(
    dbs: list[float],
    *,
    bin_sec: float = 0.05,
    thresh_db: float = -34.0,
    merge_gap_sec: float = 0.25,
) -> list[tuple[float, float]]:
    """Return active (start, end) speech segments merging gaps <= merge_gap_sec."""
    gap_bins = max(1, int(round(merge_gap_sec / bin_sec)))
    active = [db > thresh_db for db in dbs]
    segs: list[tuple[float, float]] = []
    i = 0
    while i < len(active):
        if not active[i]:
            i += 1
            continue
        start_idx = i
        last_active = i
        j = i + 1
        while j < len(active):
            if active[j]:
                last_active = j
                j += 1
            elif j - last_active <= gap_bins:
                j += 1
            else:
                break
        segs.append((round(start_idx * bin_sec, 3), round((last_active + 1) * bin_sec, 3)))
        i = last_active + 1
    return segs


def scan_audio_glitches(path: Path, *, thresh_db: float = -34.0) -> dict[str, Any]:
    """Detect leading isolated stray syllable/click and trailing silence."""
    dbs = audio_db_bins(path)
    dur = len(dbs) * 0.05
    segs = find_active_segments(dbs, thresh_db=thresh_db)
    leading_glitch = None
    recommended_start = 0.0
    if len(segs) >= 2:
        s0_start, s0_end = segs[0]
        s1_start, _ = segs[1]
        gap = round(s1_start - s0_end, 3)
        if s0_end <= 0.45 and gap >= 0.25:
            leading_glitch = {
                "start": s0_start,
                "end": s0_end,
                "gap_after": gap,
            }
            recommended_start = max(0.0, round(s1_start - 0.06, 3))
    elif segs and segs[0][0] >= 0.20:
        recommended_start = max(0.0, round(segs[0][0] - 0.06, 3))

    recommended_end = round(dur, 3)
    if segs:
        last_end = segs[-1][1]
        if dur - last_end >= 0.25:
            recommended_end = min(round(dur, 3), round(last_end + 0.14, 3))

    long_pauses = [
        {
            "start": segs[k][1],
            "end": segs[k + 1][0],
            "duration": round(segs[k + 1][0] - segs[k][1], 3),
        }
        for k in range(len(segs) - 1)
        if segs[k + 1][0] - segs[k][1] >= 0.80
    ]
    mid_speech_glitches = [
        {"start": start, "end": end}
        for index, (start, end) in enumerate(segs[1:-1], 1)
        if end - start <= 0.15 and start - segs[index - 1][1] >= 0.20
        and segs[index + 1][0] - end >= 0.20
        and max(dbs[round(start * 20):round(end * 20)] or [-100]) >= -8
    ]
    return {
        "duration": round(dur, 3),
        "segments": segs,
        "leading_glitch": leading_glitch,
        "recommended_trim": [recommended_start, recommended_end],
        "long_pauses": long_pauses,
        "mid_speech_glitches": mid_speech_glitches,
    }


def has_audio_stream(path: Path) -> bool:
    streams = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                       "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], text=True)
    return bool(streams.strip())


def accept_downloaded_shot(film: Path, shot: str, *, vlm_provider: str = "none", asr_path=None) -> dict:
    try:
        from .h3_submit import accept_downloaded_shot as accept
    except ImportError:
        from h3_submit import accept_downloaded_shot as accept
    return accept(film, shot, vlm_provider=vlm_provider, asr_path=asr_path)


def extract_shot_contact_sheet(
    film: Path,
    shot: str,
    *,
    fractions: tuple[float, ...] = FRAME_FRACTIONS,
    scale_width: int = 270,
) -> Path:
    """Extract a 4-frame horizontal contact sheet across the full shot."""
    film = Path(film)
    src = film / "shots" / f"{shot}.mp4"
    if not src.is_file():
        raise FileNotFoundError(f"missing shot video: {src}")
    dur = max(0.2, ffprobe_duration(src))
    out_dir = film / CONTACT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{shot}.jpg"

    tmp_frames: list[Path] = []
    try:
        for idx, frac in enumerate(fractions):
            t = min(max(0.05, dur * frac), max(0.05, dur - 0.05))
            fp = out_dir / f".tmp_{shot}_{idx}.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={scale_width}:-2",
                    "-q:v",
                    "3",
                    str(fp),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            tmp_frames.append(fp)

        cmd = ["ffmpeg", "-y"]
        for fp in tmp_frames:
            cmd.extend(["-i", str(fp)])
        cmd.extend(
            [
                "-filter_complex",
                f"hstack=inputs={len(tmp_frames)}[out]",
                "-map",
                "[out]",
                "-q:v",
                "3",
                str(out_path),
            ]
        )
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    finally:
        for fp in tmp_frames:
            fp.unlink(missing_ok=True)
    return out_path


def inspect_film_shots(film: Path, shot_id: str | None = None) -> list[dict[str, Any]]:
    """Extract contact sheets and scan audio glitches for all (or one) shot."""
    film = Path(film)
    plan = film / "qa" / "REBEAT-PLAN.json"
    if shot_id:
        shot_ids = [shot_id]
    elif plan.is_file():
        shot_ids = [s["id"] for s in (json.loads(plan.read_text()).get("shots") or [])]
    else:
        shot_ids = sorted(p.stem for p in (film / "shots").glob("S*.mp4"))

    results = []
    for sid in shot_ids:
        vid = film / "shots" / f"{sid}.mp4"
        if not vid.is_file():
            results.append({"shot": sid, "missing": True})
            continue
        sheet = extract_shot_contact_sheet(film, sid)
        audio = scan_audio_glitches(vid)
        results.append(
            {
                "shot": sid,
                "video": str(vid.relative_to(film)),
                "shot_sha256": file_sha256(vid),
                "contact_sheet": str(sheet.relative_to(film)),
                **audio,
            }
        )
    return results


def build_visual_lock(
    shots: dict[str, Any] | None = None,
    *,
    note: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "rule": (
            "Inspect multi-timestamp contact sheet across the full generated shot before PASS. "
            "Must verify size stability (no shrink/bloat), product identity, no baked captions, "
            "no hallucinated SKU tags, and presenter continuity."
        ),
        "shots": dict(shots or {}),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if note:
        data["note"] = note
    return data


def apply_video_review(
    film: Path,
    data: dict[str, Any],
    shot: str,
    *,
    size_stable: bool,
    size_vs_spec: str = "match",
    identity_intact: bool = True,
    no_baked_captions: bool = True,
    no_hallucinated_tags: bool = True,
    person_consistent: bool = True,
    reviewer: str = "manual",
    status: str = "PASS",
    note: str = "",
) -> dict[str, Any]:
    """Record structured post-H3 visual review bound to current shot_sha256."""
    film = Path(film)
    vid = film / "shots" / f"{shot}.mp4"
    if not vid.is_file():
        raise FileNotFoundError(f"missing shot video: {vid}")
    sheet = film / CONTACT_DIR_NAME / f"{shot}.jpg"
    if not sheet.is_file():
        sheet = extract_shot_contact_sheet(film, shot)

    vs_spec = size_vs_spec.strip().lower()
    if vs_spec not in VALID_SIZE_VS_SPEC and status.upper() == "PASS":
        raise ValueError("size_vs_spec must be 'match' for PASS")

    all_ok = all(
        [
            size_stable,
            vs_spec in VALID_SIZE_VS_SPEC,
            identity_intact,
            no_baked_captions,
            no_hallucinated_tags,
            person_consistent,
        ]
    )
    final_status = status.upper()
    if final_status == "PASS" and not all_ok:
        raise ValueError("Cannot mark PASS when a visual check flag is false/mismatch")

    shots = dict(data.get("shots") or {})
    entry: dict[str, Any] = {
        "status": final_status,
        "reviewer": reviewer,
        "shot_sha256": file_sha256(vid),
        "contact_sheet": str(sheet.relative_to(film)),
        "size_stable": bool(size_stable),
        "size_vs_spec": vs_spec,
        "identity_intact": bool(identity_intact),
        "no_baked_captions": bool(no_baked_captions),
        "no_hallucinated_tags": bool(no_hallucinated_tags),
        "person_consistent": bool(person_consistent),
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if note:
        entry["note"] = note
    shots[shot] = entry
    out = dict(data)
    out["shots"] = shots
    out["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out


def validate_visual_lock_data(
    data: dict[str, Any],
    film: Path,
    shots: list,
    *,
    require_vlm_reviewer: bool = True,
) -> list[str]:
    errs: list[str] = []
    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        entry = shot_map.get(sid)
        if not isinstance(entry, dict):
            errs.append(
                f"H3-VISUAL-LOCK.json: {sid} must be a structured review object "
                "(run: replica-pipeline vlm-review-video ...)"
            )
            continue
        if require_vlm_reviewer and not str(entry.get("reviewer") or "").startswith("vlm:"):
            errs.append(
                f"H3-VISUAL-LOCK.json: {sid} reviewer must start with 'vlm:' "
                "(run independent vlm-review-video; self-signing forbidden)"
            )
        if str(entry.get("status") or "").upper() != "PASS":
            errs.append(f"H3-VISUAL-LOCK.json: {sid} visual review not PASS")
            continue
        for flag in (
            "size_stable",
            "identity_intact",
            "no_baked_captions",
            "no_hallucinated_tags",
            "person_consistent",
        ):
            if entry.get(flag) is not True:
                errs.append(f"H3-VISUAL-LOCK.json: {sid} {flag} must be true")
        if str(entry.get("size_vs_spec") or "").strip().lower() not in VALID_SIZE_VS_SPEC:
            errs.append(f"H3-VISUAL-LOCK.json: {sid} size_vs_spec must be 'match'")

        sheet_rel = entry.get("contact_sheet")
        if not sheet_rel or not (film / sheet_rel).is_file():
            errs.append(f"H3-VISUAL-LOCK.json: {sid} contact_sheet missing on disk: {sheet_rel}")

        vid = film / "shots" / f"{sid}.mp4"
        expected_hash = entry.get("shot_sha256")
        if not expected_hash:
            errs.append(f"H3-VISUAL-LOCK.json: {sid} missing shot_sha256")
        elif vid.is_file() and file_sha256(vid) != expected_hash:
            errs.append(
                f"H3-VISUAL-LOCK.json: {sid} shot_sha256 mismatch — video changed since visual review"
            )
    return errs
