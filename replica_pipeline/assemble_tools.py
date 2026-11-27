"""ASR-word-aligned trimming, caption alignment, and assemble validation.

Fixes three assembly failure modes observed in production:
1. Stray leading syllables/clicks before pauses (e.g. 0.05s "下" + 3s silence).
2. Linearly spaced planned-script captions that drift from actual spoken timing.
3. Undersized captions on 1080p/1152p vertical frames.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from .video_qa import audio_db_bins, find_active_segments, scan_audio_glitches
except ImportError:
    from video_qa import audio_db_bins, find_active_segments, scan_audio_glitches

PUNCTUATION = set("，。、！？,.!? ")


def recommended_font_size(width: int) -> int:
    """Scale caption font proportionally (~56px at 1152w, ~28px at 576w)."""
    return max(26, round(width * 56 / 1152))


def min_font_size(width: int) -> int:
    """Minimum acceptable caption height (~50px at 1152w, ~25px at 576w)."""
    return max(24, round(width * 0.043))


def asr_chars(asr_path: Path) -> list[dict[str, Any]]:
    """Extract non-punctuation character/word entries with timestamps from WhisperX JSON."""
    body = json.loads(asr_path.read_text())
    chars: list[dict[str, Any]] = []
    for seg in body.get("segments") or []:
        for w in seg.get("words") or []:
            text = (w.get("word") or w.get("text") or "").strip()
            if text and text not in PUNCTUATION:
                chars.append(
                    {
                        "text": text,
                        "start": float(w.get("start") or 0),
                        "end": float(w.get("end") or 0),
                    }
                )
    return chars


def detect_trim_window(
    video_path: Path,
    chars: list[dict[str, Any]],
    *,
    pad_in: float = 0.08,
    pad_out: float = 0.16,
    head_keep: float = 0.16,
    tail_keep: float = 0.25,
) -> dict[str, Any]:
    """Compute clean [t0, t1] window, skipping isolated leading glitch bursts and silence."""
    audio_info = scan_audio_glitches(video_path)
    dur = audio_info["duration"]
    segs = audio_info["segments"]
    glitch = audio_info["leading_glitch"]

    skip_head_chars = 0
    if glitch and len(segs) >= 2:
        main_start = segs[1][0]
        # Skip ASR chars that fell inside the isolated leading glitch burst
        skip_head_chars = sum(1 for c in chars if c["start"] < glitch["end"] + 0.05)
        used = chars[skip_head_chars:] or chars
        speech_start = max(main_start, used[0]["start"])
        t0 = max(0.0, speech_start - pad_in)
    else:
        used = chars
        speech_start = used[0]["start"] if used else (segs[0][0] if segs else 0.0)
        t0 = max(0.0, speech_start - pad_in)
        if t0 < head_keep and not (segs and segs[0][0] >= 0.16):
            t0 = 0.0

    speech_end = used[-1]["end"] if used else (segs[-1][1] if segs else dur)
    if segs:
        speech_end = min(speech_end, segs[-1][1] + 0.05)
    t1 = min(dur, speech_end + pad_out)
    if dur - t1 < tail_keep:
        t1 = dur
    if t1 <= t0:
        t1 = min(dur, t0 + 0.8)

    return {
        "t0": round(t0, 3),
        "t1": round(t1, 3),
        "skip_head_chars": skip_head_chars,
        "leading_glitch": glitch,
        "duration": dur,
    }


def _find_voice_end(dbs: list[float], last_char_start: float, next_bound: float) -> float:
    i0 = max(0, int((last_char_start + 0.05) / 0.05))
    i1 = min(len(dbs), int(next_bound / 0.05))
    if i1 <= i0:
        return min(next_bound, last_char_start + 0.25)
    quiet = 0
    last_loud = i0
    for idx in range(i0, i1):
        if dbs[idx] > -33.0:
            last_loud = idx
            quiet = 0
        else:
            quiet += 1
            if quiet >= 3 and idx > i0 + 1:
                break
    return min(next_bound, (last_loud + 1) * 0.05)


def align_phrases_to_asr(
    video_path: Path,
    chars: list[dict[str, Any]],
    phrases: list[tuple[str, int] | str],
    *,
    trim_t0: float,
    clip_dur: float,
    offset: float = 0.0,
    shot: str = "",
    skip_head: int = 0,
    skip_tail: int = 0,
) -> list[dict[str, Any]]:
    """Align spoken phrases 1-to-1 with ASR char timestamps + audio energy end detection."""
    norm_phrases: list[tuple[str, int]] = []
    for item in phrases:
        if isinstance(item, tuple):
            norm_phrases.append((item[0], int(item[1])))
        else:
            clean = "".join(ch for ch in item if ch not in PUNCTUATION)
            norm_phrases.append((item, len(clean)))

    end_idx = len(chars) - skip_tail if skip_tail else len(chars)
    used = chars[skip_head:end_idx]
    expected = sum(cnt for _, cnt in norm_phrases)
    if expected != len(used):
        raise ValueError(
            f"{shot}: phrase char count {expected} != ASR chars {len(used)} "
            f"({''.join(c['text'] for c in used)})"
        )

    dbs = audio_db_bins(video_path)
    spans: list[list[Any]] = []
    pos = 0
    for text, cnt in norm_phrases:
        first_c = used[pos]
        last_c = used[pos + cnt - 1]
        pos += cnt
        next_start = used[pos]["start"] if pos < len(used) else (trim_t0 + clip_dur)
        voice_end = _find_voice_end(dbs, last_c["start"], next_start)
        s_local = max(0.0, first_c["start"] - trim_t0 - 0.04)
        e_local = min(clip_dur, max(s_local + 0.25, voice_end - trim_t0 + 0.10))
        spans.append([text, s_local, e_local])

    for i in range(len(spans) - 1):
        nxt = spans[i + 1][1]
        if spans[i][2] > nxt - 0.03:
            spans[i][2] = max(spans[i][1] + 0.20, nxt - 0.03)

    return [
        {
            "start": round(offset + s, 3),
            "end": round(offset + e, 3),
            "text": text,
            "shot": shot,
        }
        for text, s, e in spans
    ]


def validate_assemble_data(film: Path) -> list[str]:
    """Validate assemble-check.json & output video properties for 6_assemble."""
    errs: list[str] = []
    chk_path = film / "qa" / "assemble-check.json"
    if not chk_path.is_file():
        return ["qa/assemble-check.json required"]
    try:
        chk = json.loads(chk_path.read_text())
    except Exception as e:
        return [f"qa/assemble-check.json: {e}"]

    bgm_src = str(chk.get("bgm_source") or "").strip().lower()
    if bgm_src not in {"source", "new", "none"}:
        errs.append("assemble-check.json: bgm_source must be 'source', 'new', or 'none'")

    has_captions = chk.get("has_captions", True)
    if has_captions:
        align = str(chk.get("caption_align") or "").strip().lower()
        if align != "asr_words":
            errs.append(
                "assemble-check.json: caption_align must be 'asr_words' "
                "(linear uniform cue splitting causes caption drift)"
            )
        width = int(chk.get("width") or 0)
        font_px = int(chk.get("caption_font_px") or 0)
        if width <= 0:
            cap_mp4 = film / "qa" / "captioned.mp4"
            if cap_mp4.is_file():
                try:
                    probe = subprocess.check_output(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=width",
                            "-of",
                            "csv=p=0",
                            str(cap_mp4),
                        ],
                        text=True,
                    ).strip()
                    width = int(probe)
                except Exception:
                    width = 1152
            else:
                width = 1152
        req_px = min_font_size(width)
        if font_px < req_px:
            errs.append(
                f"assemble-check.json: caption_font_px={font_px} too small for width={width} "
                f"(require >= {req_px}px, recommended {recommended_font_size(width)}px)"
            )
        cues_rel = chk.get("caption_cues_path")
        if not cues_rel or not (film / cues_rel).is_file():
            errs.append(f"assemble-check.json: caption_cues_path missing on disk: {cues_rel}")

    if chk.get("leading_glitch_trimmed") is not True:
        errs.append(
            "assemble-check.json: leading_glitch_trimmed must be true "
            "(run audio glitch trim before concat)"
        )
    return errs
