"""ASR-word-aligned trimming, Douyin caption formatting, BGM ducking, and assembly.

Absorbs Hypit sound-mix and caption craft directly into replica-pipeline:
1. Stray leading syllable/click trimming (`detect_trim_window` + fade-in/fade-out).
2. ASR-word-aligned cue timing (`align_phrases_to_asr`), preventing linear drift.
3. Douyin caption normalization (`format_douyin_caption`: strip trailing sentence
   punctuation, clean line wrapping) and scaled typography (56px @ 1152w).
4. Speech-first audio mix (`loudnorm` + `sidechaincompress` BGM ducking + fades).
5. End-to-end `assemble_film` emitting `qa/strict-with-bgm.mp4`, `qa/captioned.mp4`,
   `qa/captions.asr-cues.json`, `qa/captions.ass`, and `qa/assemble-check.json`.
"""
from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from .asr_verify import timed_tokens
    from .gate import require as pipeline_require
    from .script_lint import DIALOGUE_RE, SHOT_RE, audio_windows, compile_shot_text, plan_visual_durations, project_dual_text, read_strict_json, speech_chars
    from .video_qa import (
        audio_db_bins,
        ffprobe_duration,
        file_sha256,
        find_active_segments,
        scan_audio_glitches,
    )
except ImportError:
    from asr_verify import timed_tokens
    from gate import require as pipeline_require
    from script_lint import DIALOGUE_RE, SHOT_RE, audio_windows, compile_shot_text, plan_visual_durations, project_dual_text, read_strict_json, speech_chars
    from video_qa import (
        audio_db_bins,
        ffprobe_duration,
        file_sha256,
        find_active_segments,
        scan_audio_glitches,
    )

PUNCTUATION = set("，。、！？,.!? ")
TRAILING_CAPTION_PUNCT = "。，、！？,.!?…;； "
CLAUSE_SPLIT_RE = re.compile(r"[，。！？；,.!?\n]+")
STRIP_METADATA = ["-map_metadata", "-1", "-map_chapters", "-1", "-map_metadata:s", "-1"]


def recommended_font_size(width: int) -> int:
    """Scale caption font proportionally (~56px at 1152w, ~28px at 576w)."""
    return max(26, round(width * 56 / 1152))


def min_font_size(width: int) -> int:
    """Minimum acceptable caption height (~50px at 1152w, ~25px at 576w)."""
    return max(24, round(width * 0.043))


def format_douyin_caption(text: str, *, max_line_chars: int = 13) -> str:
    """Format one Cue for Douyin/vertical social playback (Hypit captions craft).

    - Resolves Dual Text (`<display|spoken>` -> `display`) and strips `||`.
    - Removes trailing sentence punctuation (`。，！？,.!?`) and converts inner
      commas to clean thin spaces so captions read fast on mobile.
    - Wraps into at most 2 balanced lines if longer than `max_line_chars`.
    """
    disp = project_dual_text(str(text or ""), side="display").replace("||", " ").strip()
    disp = disp.rstrip(TRAILING_CAPTION_PUNCT)
    disp = re.sub(r"[，,、]+", " ", disp)
    disp = re.sub(r"\s{2,}", " ", disp).strip()
    if not disp or len(disp) <= max_line_chars:
        return disp

    # Prefer splitting near midpoint on whitespace, else clean character midpoint.
    mid = len(disp) // 2
    space_idxs = [i for i, ch in enumerate(disp) if ch == " "]
    if space_idxs:
        best = min(space_idxs, key=lambda idx: abs(idx - mid))
        left, right = disp[:best].strip(), disp[best + 1 :].strip()
        if left and right:
            return f"{left}\n{right}"
    return f"{disp[:mid].strip()}\n{disp[mid:].strip()}"


def _ass_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60.0
    cs = int(round((s - int(s)) * 100))
    si = int(s)
    if cs >= 100:
        cs -= 100
        si += 1
    return f"{h}:{m:02d}:{si:02d}.{cs:02d}"


def build_ass_subtitles(
    cues: list[dict[str, Any]],
    *,
    width: int = 1152,
    height: int = 2048,
    font_name: str = "PingFang SC",
    max_line_chars: int = 13,
) -> str:
    """Build a styled ASS subtitle document sized proportionally to canvas width."""
    font_px = recommended_font_size(width)
    margin_v = max(64, round(height * 0.17))
    margin_lr = max(48, round(width * 0.06))
    outline = max(2, round(font_px / 16))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font_name},{font_px},&H00FFFFFF,&H0000FFFF,"
            f"&H00121216,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},1,"
            f"2,{margin_lr},{margin_lr},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in cues:
        formatted = format_douyin_caption(c.get("text", ""), max_line_chars=max_line_chars)
        if not formatted:
            continue
        ass_text = formatted.replace("\n", r"\N")
        t0 = _ass_time(float(c.get("start") or 0.0))
        t1 = _ass_time(float(c.get("end") or 0.0))
        lines.append(f"Dialogue: 0,{t0},{t1},Default,,0,0,0,,{ass_text}")
    return "\n".join(lines) + "\n"


def build_ducking_filter(
    *,
    total_dur: float,
    bgm_db: float = -14.0,
    fade_in: float = 0.6,
    fade_out: float = 1.2,
    lookahead_ms: float = 180.0,
    release_ms: float = 800.0,
    threshold: float = 0.04,
    ratio: float = 3.5,
) -> str:
    """Build ffmpeg filter_complex normalizing speech (-16 LUFS) and ducking BGM underneath."""
    total_dur = max(0.5, float(total_dur))
    fi = min(float(fade_in), total_dur * 0.35)
    fo = min(float(fade_out), total_dur * 0.45)
    fade_out_st = max(0.0, total_dur - fo)
    if not (0 <= lookahead_ms <= 2000 and 0.01 <= release_ms <= 9000 and 0.00097563 <= threshold <= 1 and 1 <= ratio <= 20):
        raise ValueError("ducking parameters out of range")
    delay = lookahead_ms / 1000
    return (
        "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,"
        "asplit=2[vo_raw][detect_raw];"
        f"[vo_raw]adelay={lookahead_ms:g}:all=1[vo_delayed];"
        f"[detect_raw]apad=pad_dur={delay:.6f}[vo_detect];"
        f"[1:a]atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
        f"aresample=48000,aformat=channel_layouts=stereo,"
        f"volume={bgm_db:.1f}dB,"
        f"afade=t=in:st=0:d={fi:.2f},"
        f"afade=t=out:st={fade_out_st:.3f}:d={fo:.2f},adelay={lookahead_ms:g}:all=1[bgm_delayed];"
        "[bgm_delayed][vo_detect]sidechaincompress="
        f"threshold={threshold:g}:ratio={ratio:g}:attack=40:release={release_ms:g}:makeup=1[bgm_ducked];"
        "[vo_delayed][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=0.25:normalize=0,"
        f"atrim=start={delay:.6f}:end={total_dur + delay:.6f},asetpts=PTS-STARTPTS[aout]"
    )


def _audio_has_signal(video_path: Path, *, min_db: float = -75.0) -> bool:
    try:
        dbs = audio_db_bins(video_path)
        return any(db > min_db for db in dbs)
    except Exception:
        return False


def mix_audio_with_ducking(
    video_path: Path,
    bgm_path: Path,
    out_path: Path,
    *,
    bgm_db: float = -14.0,
) -> Path:
    """Mux normalized voice with sidechain-ducked looped BGM (or pure BGM on silent tracks)."""
    video_path = Path(video_path)
    bgm_path = Path(bgm_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, ffprobe_duration(video_path))
    if _audio_has_signal(video_path):
        fc = build_ducking_filter(total_dur=dur, bgm_db=bgm_db)
    else:
        # Pure digital silence crashes loudnorm with NaN; lay trimmed/faded BGM cleanly.
        fi = min(0.6, dur * 0.35)
        fo = min(1.2, dur * 0.45)
        fade_out_st = max(0.0, dur - fo)
        fc = (
            f"[1:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={bgm_db:.1f}dB,"
            f"afade=t=in:st=0:d={fi:.2f},"
            f"afade=t=out:st={fade_out_st:.3f}:d={fo:.2f}[aout]"
        )
    tmp = out_path.with_suffix(".part.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            str(bgm_path),
            "-filter_complex",
            fc,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "160k",
            "-shortest",
            *STRIP_METADATA,
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    tmp.replace(out_path)
    return out_path


def _ffmpeg_has_filter(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-filters"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return re.search(rf"\b{re.escape(name)}\b", out) is not None
    except Exception:
        return False


def _find_pil_runner() -> list[str] | None:
    """Locate a Python command with Pillow installed (including uv offline cache fallback)."""
    candidates: list[list[str]] = [
        [sys.executable],
        *( [[p]] if (p := shutil.which("python3")) and p != sys.executable else [] ),
        ["/usr/bin/python3"],
        ["/opt/homebrew/bin/python3"],
    ]
    uv = shutil.which("uv")
    if uv:
        candidates.append([uv, "run", "--offline", "--with", "pillow", "python3"])
        candidates.append([uv, "run", "--with", "pillow", "python3"])

    for runner in candidates:
        try:
            subprocess.run(
                [*runner, "-c", "import PIL.Image, PIL.ImageDraw, PIL.ImageFont"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=20,
            )
            return runner
        except Exception:
            continue
    return None


def _render_cues_with_pil(
    pil_runner: list[str],
    cues: list[dict[str, Any]],
    work_dir: Path,
    *,
    width: int,
    height: int,
) -> list[tuple[Path, float, float]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_path = work_dir / "cues_render.json"
    spec_path.write_text(json.dumps(cues, ensure_ascii=False))
    font_px = recommended_font_size(width)
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from PIL import Image, ImageDraw, ImageFont\n"
        "spec = json.loads(Path(sys.argv[1]).read_text())\n"
        "out_dir = Path(sys.argv[2])\n"
        "W, H, fpx = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])\n"
        "font_cands = [\n"
        "  '/System/Library/Fonts/PingFang.ttc',\n"
        "  '/System/Library/Fonts/STHeiti Medium.ttc',\n"
        "  '/System/Library/Fonts/Hiragino Sans GB.ttc',\n"
        "]\n"
        "fnt = None\n"
        "for fp in font_cands:\n"
        "  if Path(fp).is_file():\n"
        "    fnt = ImageFont.truetype(fp, fpx)\n"
        "    break\n"
        "if fnt is None:\n"
        "  fnt = ImageFont.load_default()\n"
        "out = []\n"
        "for i, c in enumerate(spec):\n"
        "  txt = c.get('formatted') or c.get('text') or ''\n"
        "  if not txt.strip(): continue\n"
        "  img = Image.new('RGBA', (W, H), (0, 0, 0, 0))\n"
        "  draw = ImageDraw.Draw(img)\n"
        "  lines = txt.splitlines()\n"
        "  lh = fpx + 12\n"
        "  y = int(H * 0.86) - lh * len(lines)\n"
        "  for ln in lines:\n"
        "    tw = draw.textlength(ln, font=fnt)\n"
        "    x = (W - tw) / 2\n"
        "    for dx, dy in ((-3,0),(3,0),(0,-3),(0,3),(-2,-2),(2,2),(-2,2),(2,-2)):\n"
        "      draw.text((x+dx, y+dy), ln, font=fnt, fill=(10, 10, 14, 235))\n"
        "    draw.text((x, y), ln, font=fnt, fill=(255, 255, 255, 255))\n"
        "    y += lh\n"
        "  p = out_dir / f'cue_{i:03d}.png'\n"
        "  img.save(p)\n"
        "  out.append([str(p), float(c['start']), float(c['end'])])\n"
        "print(json.dumps(out))\n"
    )
    rendered = subprocess.check_output(
        [*pil_runner, "-c", script, str(spec_path), str(work_dir), str(width), str(height), str(font_px)],
        text=True,
    )
    return [(Path(p), float(s), float(e)) for p, s, e in json.loads(rendered)]


def _clean_remux(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(".part.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", *STRIP_METADATA, "-movflags", "+faststart", str(temporary)], check=True)
    temporary.replace(destination)


def burn_subtitles(
    video_path: Path,
    cues: list[dict[str, Any]],
    out_path: Path,
    *,
    width: int = 1152,
    height: int = 2048,
    ass_path: Path | None = None,
    work_dir: Path | None = None,
) -> Path:
    """Burn formatted Douyin cues via ffmpeg ASS filter or Pillow PNG overlay fallback."""
    video_path = Path(video_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    formatted_cues = [
        {**c, "formatted": format_douyin_caption(c.get("text", ""))}
        for c in cues
        if str(c.get("text") or "").strip()
    ]
    ass_file = ass_path or out_path.with_suffix(".ass")
    ass_file.write_text(build_ass_subtitles(formatted_cues, width=width, height=height))

    if not formatted_cues:
        _clean_remux(video_path, out_path)
        return out_path

    tmp = out_path.with_suffix(".part.mp4")
    if _ffmpeg_has_filter("ass"):
        esc = str(ass_file.resolve()).replace("\\", "\\\\").replace(":", "\\:")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"ass='{esc}'",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                *STRIP_METADATA,
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        tmp.replace(out_path)
        return out_path

    pil_runner = _find_pil_runner()
    if pil_runner:
        png_dir = work_dir or (out_path.parent / ".caption-pngs")
        overlays = _render_cues_with_pil(pil_runner, formatted_cues, png_dir, width=width, height=height)
        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        filters: list[str] = []
        last = "[0:v]"
        for idx, (png, start, end) in enumerate(overlays):
            cmd.extend(["-i", str(png)])
            nxt = f"[c{idx}]"
            filters.append(f"{last}[{idx + 1}:v]overlay=0:0:enable='between(t\\,{start:.3f}\\,{end:.3f})'{nxt}")
            last = nxt
        filters.append(f"{last}format=yuv420p[v]")
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "copy",
                *STRIP_METADATA,
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        )
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        tmp.replace(out_path)
        return out_path

    raise RuntimeError(
        "Cannot burn subtitles: ffmpeg lacks 'ass' filter (libass) and no Pillow runtime was found. "
        f"ASS subtitle file was written to {ass_file}"
    )


def asr_chars(asr_path: Path) -> list[dict[str, Any]]:
    """Extract non-punctuation character/word entries with timestamps from WhisperX JSON."""
    body = json.loads(asr_path.read_text())
    chars: list[dict[str, Any]] = []
    words = [word for segment in body.get("segments") or [] for word in segment.get("words") or []]
    for word in words or body.get("word_segments") or body.get("words") or []:
        text = (word.get("word") or word.get("text") or "").strip()
        start, end = word.get("start"), word.get("end")
        if speech_chars(text) and type(start) in (int, float) and type(end) in (int, float) and 0 <= start <= end:
            chars.append({"text": text, "start": float(start), "end": float(end)})
    return chars


def _projected_alignment(phrases: list[dict], chars: list[dict], *, shot: str) -> list[tuple[str, dict, dict, float | None]]:
    right_text = " ".join(row["text"] for row in chars)
    positions = [idx for idx, row in enumerate(chars) for _ in row["text"] + " "]
    right = timed_tokens(right_text)
    projected = [[token for token, _, _ in timed_tokens(row["spoken"])] for row in phrases]
    left = [token for tokens in projected for token in tokens]
    mapping = {}
    for match in SequenceMatcher(None, left, [token for token, _, _ in right], autojunk=False).get_matching_blocks():
        mapping.update({match.a + pos: match.b + pos for pos in range(match.size)})
    cursor = 0
    result = []
    for phrase, tokens in zip(phrases, projected):
        matched = [mapping[index] for index in range(cursor, cursor + len(tokens)) if index in mapping]
        cursor += len(tokens)
        if not matched or len(matched) < 0.8 * len(tokens):
            raise ValueError(f"{shot}: insufficient ASR timing matches for {phrase['spoken']!r}")
        first = chars[positions[right[matched[0]][1]]]
        last = chars[positions[right[matched[-1]][2] - 1]]
        following = matched[-1] + 1
        next_start = chars[positions[right[following][1]]]["start"] if following < len(right) else None
        result.append((phrase["display"], first, last, next_start))
    return result


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
    phrases: list[tuple[str, int] | str | dict],
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
        if isinstance(item, dict):
            continue
        if isinstance(item, tuple):
            norm_phrases.append((item[0], int(item[1])))
        else:
            clean = "".join(ch for ch in item if ch not in PUNCTUATION)
            norm_phrases.append((item, len(clean)))

    end_idx = len(chars) - skip_tail if skip_tail else len(chars)
    used = chars[skip_head:end_idx]
    expected = sum(cnt for _, cnt in norm_phrases)
    projected = bool(phrases and isinstance(phrases[0], dict))
    if not projected and expected != len(used):
        raise ValueError(
            f"{shot}: phrase char count {expected} != ASR chars {len(used)} "
            f"({''.join(c['text'] for c in used)})"
        )

    dbs = audio_db_bins(video_path)
    spans: list[list[Any]] = []
    if projected:
        aligned = _projected_alignment(phrases, used, shot=shot)
    else:
        aligned = []
        pos = 0
        for text, count in norm_phrases:
            if count > 0:
                end = pos + count
                aligned.append((text, used[pos], used[end - 1], used[end]["start"] if end < len(used) else None))
                pos = end
    for text, first_c, last_c, next_start in aligned:
        if next_start is None or next_start <= last_c["start"]:
            next_start = min(trim_t0 + clip_dur, max(last_c["end"], last_c["start"] + 0.05))
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


def _default_phrases_for_shot(film: Path, shot_obj: dict[str, Any]) -> list[dict]:
    if shot_obj.get("silent") or shot_obj.get("audio_owner", shot_obj["id"]) != shot_obj["id"]:
        return []
    sid = shot_obj["id"]
    pfile = film / "prompts" / f"{sid}.txt"
    planned = str(shot_obj.get("spoken") or shot_obj.get("vo") or "")
    prompt = pfile.read_text() if pfile.is_file() else ""
    if DIALOGUE_RE.search(prompt) and "|" in prompt and "|" not in planned:
        planned = prompt
    raw_phrases = shot_obj.get("phrases") or [planned if planned else (prompt if DIALOGUE_RE.search(prompt) else "")]
    result = []
    for raw in raw_phrases:
        compiled = [raw] if isinstance(raw, dict) else compile_shot_text(raw)["phrases"]
        for phrase in compiled:
            if phrase["display"] != phrase["spoken"]:
                if speech_chars(phrase["spoken"]):
                    result.append(phrase)
            else:
                result.extend({"display": part.strip(), "spoken": part.strip()} for part in CLAUSE_SPLIT_RE.split(phrase["spoken"].replace("||", "\n")) if speech_chars(part))
    return result


def _has_audio_stream(video_path: Path) -> bool:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return bool(out)
    except Exception:
        return True


def _encode_trimmed_clip(src: Path, dest: Path, t0: float, t1: float, *, width: int, height: int, mute: bool = False, variation: dict | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fade_out_st = max(t0, t1 - 0.03)
    has_audio = not mute and _has_audio_stream(src)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    if variation:
        zoom = variation["zoom"]
        scaled_w, scaled_h = (round(dimension * zoom / 2) * 2 for dimension in (width, height))
        vf += f",scale={scaled_w}:{scaled_h},crop={width}:{height},setsar=1,eq=contrast={variation['contrast']}:saturation={variation['saturation']}"
    if mute:
        cmd = ["ffmpeg", "-y", "-ss", f"{t0:.3f}", "-i", str(src), "-t", f"{t1-t0:.3f}", "-an", "-vf", vf]
    elif has_audio:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ss",
            f"{t0:.3f}",
            "-to",
            f"{t1:.3f}",
            "-vf",
            vf,
            "-af",
            f"afade=t=in:st={t0:.3f}:d=0.025,afade=t=out:st={fade_out_st:.3f}:d=0.03",
        ]
    else:
        dur = max(0.2, t1 - t0)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{t0:.3f}",
            "-t",
            f"{dur:.3f}",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-t",
            f"{dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            vf,
            "-shortest",
        ]
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "160k",
            *STRIP_METADATA,
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def build_assembly_timeline(film: Path, shots: list[dict], *, durations: dict[str, float] | None = None) -> dict:
    film = Path(film)
    ids = [row["id"] for row in shots]
    if not ids or not all(isinstance(sid, str) and SHOT_RE.fullmatch(sid) for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError("timeline requires nonempty unique shots")
    durations = durations if durations is not None else {sid: ffprobe_duration(film / "shots" / f"{sid}.mp4") for sid in ids}
    visual_durations = plan_visual_durations(shots, durations)
    windows = audio_windows(shots, visual_durations, source_durations=durations)
    visuals, audio = [], []
    offset = 0.0
    for row, (owner, start, end) in zip(shots, windows):
        sid = row["id"]
        length = end - start if owner == sid and "audio_out" in row else visual_durations[sid]
        if not 0 < length <= durations[sid] + 0.05:
            raise ValueError(f"{sid}: visual window out of range")
        if abs(length - (end - start)) > 0.1:
            raise ValueError(f"{sid}: visual/audio lengths disagree")
        visuals.append({"shot": sid, "start": offset, "end": offset + length, "duration": length})
        silent = bool(row.get("silent")) and owner == sid
        if (audio and audio[-1]["owner"] == owner and not silent and not audio[-1]["silent"]
                and abs(audio[-1]["audio_out"] - start) <= 0.001):
            audio[-1].update(audio_out=end, end=offset + length)
        else:
            audio.append({"owner": owner, "audio_in": start, "audio_out": end, "start": offset,
                          "end": offset + length, "first_shot": sid, "silent": silent})
        offset += length
    return {"visuals": visuals, "audio": audio, "duration": offset}


def _mux_timeline_audio(film: Path, video: Path, timeline: dict, destination: Path) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    filters = []
    leading_trim = {}
    for idx, span in enumerate(timeline["audio"], 1):
        src = film / "shots" / f"{span['owner']}.mp4"
        duration = span["end"] - span["start"]
        if span["silent"]:
            cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
            start, end = 0, duration
            suppress_until = 0.0
        else:
            if not _has_audio_stream(src):
                raise ValueError(f"{span['owner']}: missing owner audio track")
            cmd.extend(["-i", str(src)])
            start, end = span["audio_in"], span["audio_out"]
            if span["owner"] not in leading_trim:
                scan = scan_audio_glitches(src)
                leading_trim[span["owner"]] = scan["recommended_trim"][0] if scan.get("leading_glitch") else 0.0
            suppress_until = max(0.0, leading_trim[span["owner"]] - start)
        reduction = f",volume=0:enable='lt(t,{suppress_until:.6f})'" if suppress_until else ""
        filters.append(f"[{idx}:a:0]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                       f"aformat=sample_rates=48000:channel_layouts=stereo{reduction},apad,atrim=duration={duration:.6f}[a{idx}]")
    count = len(filters)
    filters.append("".join(f"[a{idx}]" for idx in range(1, count + 1)) + f"concat=n={count}:v=0:a=1[aout]")
    cmd.extend(["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", *STRIP_METADATA, str(destination)])
    subprocess.run(cmd, check=True)


def assemble_film(
    film: Path,
    *,
    bgm_path: Path | None = None,
    bgm_source: str = "none",
    bgm_db: float | None = None,
    asr_dir: Path | None = None,
    width: int = 1152,
    height: int = 2048,
    skip_gate: bool = False,
    allow_uncaptioned: bool = False,
    language: str = "zh",
    differentiate: bool = False,
    matrix_seed: str | int | None = None,
) -> dict[str, Any]:
    """Trim glitches, concatenate shots, duck BGM, align ASR cues, and emit final deliverables."""
    film = Path(film)
    if not skip_gate:
        pipeline_require(film, "assemble")
        if width <= 0 or height <= 0 or width * 16 != height * 9 or width % 2 or height % 2:
            raise ValueError("assembly requires even 9:16 output dimensions")

    plan_path = film / "qa" / "REBEAT-PLAN.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"missing {plan_path}")
    shots = json.loads(plan_path.read_text()).get("shots") or []
    ids = [row["id"] for row in shots]
    if not ids or not all(isinstance(sid, str) and SHOT_RE.fullmatch(sid) for sid in ids) or len(ids) != len(set(ids)):
        raise ValueError("REBEAT-PLAN.json requires nonempty unique valid shot IDs")
    spanning = any({"audio_owner", "audio_in", "audio_out", "visual_sec"} & row.keys() for row in shots)
    timeline = build_assembly_timeline(film, shots) if spanning else None
    variation = None
    if differentiate or matrix_seed is not None:
        seed = str(matrix_seed if matrix_seed is not None else film.name)
        digest = hashlib.sha256(seed.encode()).digest()
        variation = {"seed": seed, "zoom": round(1.01 + digest[0] / 255 * 0.02, 5),
                     "contrast": round(0.97 + digest[1] / 255 * 0.06, 5),
                     "saturation": round(0.97 + digest[2] / 255 * 0.06, 5)}

    work = film / "qa" / "assemble-work"
    if work.exists():
        shutil.rmtree(work)
    clips_dir = work / "clips"
    clips_dir.mkdir(parents=True)
    lock_path = film / "qa/H3-MEANING.json"
    meaning = read_strict_json(lock_path)["shots"] if not skip_gate else {}
    inputs = {str(plan_path.relative_to(film)): file_sha256(plan_path)}

    trim_jobs: list[tuple[dict[str, Any], Path, Path, list[dict[str, Any]], dict[str, Any], list[dict]]] = []
    for s in shots:
        sid = s["id"]
        src = film / "shots" / f"{sid}.mp4"
        if not src.is_file():
            raise FileNotFoundError(f"missing shot video: {src}")
        inputs[str(src.relative_to(film))] = file_sha256(src)
        asr_cand = (Path(asr_dir) / f"{sid}.json") if asr_dir else (film / "qa" / "asr" / f"{sid}.json")
        phrases = _default_phrases_for_shot(film, s)
        if sid in meaning and phrases:
            evidence = meaning[sid]
            if asr_dir is None:
                asr_cand = film / evidence["asr_path"]
            if file_sha256(asr_cand) != evidence["asr_sha256"]:
                raise ValueError(f"{sid}: assembly ASR must match accepted meaning evidence")
        if phrases and asr_cand.is_file():
            inputs[str(asr_cand.resolve())] = file_sha256(asr_cand)
        if not asr_cand.is_file() and phrases and shutil.which("hypit"):
            asr_cand.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["hypit", "transcribe", str(src), "--language", language, "--to", str(asr_cand)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if phrases and not asr_cand.is_file() and not allow_uncaptioned:
            raise FileNotFoundError(
                f"{sid}: shot has spoken phrases ({len(phrases)}) but missing ASR JSON {asr_cand}; "
                "provide --asr-dir / hypit transcribe or pass --allow-uncaptioned explicitly"
            )
        chars = asr_chars(asr_cand) if phrases and asr_cand.is_file() else []
        if phrases and not chars and (not allow_uncaptioned or not skip_gate):
            raise ValueError(f"{sid}: verified ASR needs usable word timestamps for captions")
        if timeline:
            length = timeline["visuals"][len(trim_jobs)]["duration"]
            win = {"t0": 0.0, "t1": length, "skip_head_chars": 0}
        elif _has_audio_stream(src):
            win = detect_trim_window(src, chars)
        else:
            sdur = round(ffprobe_duration(src), 3)
            win = {"t0": 0.0, "t1": sdur, "skip_head_chars": 0, "leading_glitch": None, "duration": sdur}
        trim_jobs.append((s, src, clips_dir / f"{sid}.mp4", chars, win, phrases))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [
            pool.submit(_encode_trimmed_clip, src, dest, win["t0"], win["t1"], width=width, height=height, mute=spanning, variation=variation)
            for _, src, dest, _, win, _ in trim_jobs
        ]
        for fut in futs:
            fut.result()

    concat_list = work / "concat.txt"
    concat_list.write_text("\n".join(f"file 'clips/{dest.name}'" for _, _, dest, _, _, _ in trim_jobs) + "\n")
    raw_concat = work / "concat.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", *STRIP_METADATA, str(raw_concat)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    if timeline:
        muxed = work / "timeline.mp4"
        _mux_timeline_audio(film, raw_concat, timeline, muxed)
        raw_concat = muxed

    strict_bgm = film / "qa" / "strict-with-bgm.mp4"
    bgm_file = Path(bgm_path) if bgm_path else next((path for path in sorted((film / "qa").glob("bgm.*"))
                                                    if path.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}), None)
    if bgm_path is not None and not bgm_file.is_file():
        raise FileNotFoundError(bgm_file)
    eff_bgm_source = bgm_source if bgm_source in {"source", "new", "none"} else "none"
    has_spoken = any(phrases for _, _, _, _, _, phrases in trim_jobs)
    eff_bgm_db = float(bgm_db) if bgm_db is not None else (-14.0 if has_spoken else -4.0)
    if bgm_file and bgm_file.is_file():
        inputs[str(bgm_file.resolve())] = file_sha256(bgm_file)
        mix_audio_with_ducking(raw_concat, bgm_file, strict_bgm, bgm_db=eff_bgm_db)
        if eff_bgm_source == "none":
            eff_bgm_source = "source"
    else:
        _clean_remux(raw_concat, strict_bgm)

    cues: list[dict[str, Any]] = []
    offset = 0.0
    for s, src, dest, chars, win, phrases in ([] if timeline else trim_jobs):
        sid = s["id"]
        clip_dur = ffprobe_duration(dest)
        if chars and phrases:
            cues.extend(
                align_phrases_to_asr(
                    src,
                    chars,
                    phrases,
                    trim_t0=win["t0"],
                    clip_dur=clip_dur,
                    offset=offset,
                    shot=sid,
                    skip_head=win["skip_head_chars"],
                )
            )
        offset += clip_dur
    if timeline:
        owner_cues = {}
        for row, src, _, chars, _, phrases in trim_jobs:
            if chars and phrases:
                owner_cues[row["id"]] = align_phrases_to_asr(src, chars, phrases, trim_t0=0,
                                                           clip_dur=ffprobe_duration(src), shot=row["id"])
        for span in timeline["audio"]:
            for cue in owner_cues.get(span["owner"], []):
                start = max(cue["start"], span["audio_in"])
                end = min(cue["end"], span["audio_out"])
                if end > start:
                    shift = span["start"] - span["audio_in"]
                    cues.append({**cue, "start": round(start + shift, 3), "end": round(end + shift, 3)})
        offset = timeline["duration"]

    if not cues and (not allow_uncaptioned or (has_spoken and not skip_gate)):
        raise ValueError(
            "No caption cues produced (fail-closed). Check qa/asr/Sxx.json & spoken text, "
            "or pass --allow-uncaptioned for an explicitly silent video."
        )

    cues_path = film / "qa" / "captions.asr-cues.json"
    cues_path.write_text(json.dumps(cues, ensure_ascii=False, indent=2) + "\n")
    ass_path = film / "qa" / "captions.ass"
    captioned = film / "qa" / "captioned.mp4"

    if cues:
        burn_subtitles(strict_bgm, cues, captioned, width=width, height=height, ass_path=ass_path, work_dir=work / "pngs")
        align_mode = "asr_words"
    else:
        ass_path.write_text(build_ass_subtitles([], width=width, height=height))
        _clean_remux(strict_bgm, captioned)
        align_mode = "none"

    chk = {
        "bgm_source": eff_bgm_source,
        "bgm_ducked": bool(bgm_file and bgm_file.is_file()),
        "has_captions": bool(cues),
        "explicit_silent": bool(not cues and allow_uncaptioned),
        "cues_count": len(cues),
        "caption_align": align_mode,
        "width": width,
        "height": height,
        "caption_font_px": recommended_font_size(width),
        "caption_cues_path": "qa/captions.asr-cues.json",
        "leading_glitch_trimmed": True,
        "shots_count": len(trim_jobs),
        "total_duration": round(offset, 3),
        "assembled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "matrix_variation": variation,
        "audio_timeline": timeline,
        "bgm_path": str(bgm_file.resolve()) if bgm_file and bgm_file.is_file() else None,
        "output_sha256": {path.name: file_sha256(path) for path in (captioned, strict_bgm, cues_path)},
        "input_sha256": inputs,
    }
    (film / "qa" / "assemble-check.json").write_text(json.dumps(chk, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "film": film.name, "captioned": str(captioned), "assemble_check": chk}


def validate_assemble_data(film: Path, *, strict: bool = False) -> list[str]:
    """Validate assemble-check.json & output video properties for 6_assemble."""
    errs: list[str] = []
    chk_path = film / "qa" / "assemble-check.json"
    if not chk_path.is_file():
        return ["qa/assemble-check.json required"]
    try:
        chk = json.loads(chk_path.read_text())
    except Exception as e:
        return [f"qa/assemble-check.json: {e}"]

    if strict:
        plan_path = film / "qa/REBEAT-PLAN.json"
        expected_inputs = {"qa/REBEAT-PLAN.json"} | {f"shots/{row['id']}.mp4" for row in read_strict_json(plan_path)["shots"]}
        inputs, outputs = chk.get("input_sha256") or {}, chk.get("output_sha256") or {}
        if not expected_inputs <= inputs.keys() or not {"captioned.mp4", "strict-with-bgm.mp4", "captions.asr-cues.json"} <= outputs.keys():
            errs.append("assemble-check.json: incomplete input/output hash bindings; reassemble")
        for folder, bindings in ((film, inputs), (film / "qa", outputs)):
            for rel, recorded in bindings.items():
                path = folder / rel
                if not path.is_file() or file_sha256(path) != recorded:
                    errs.append(f"assemble-check.json: stale {rel}; reassemble")

    bgm_src = str(chk.get("bgm_source") or "").strip().lower()
    if bgm_src not in {"source", "new", "none"}:
        errs.append("assemble-check.json: bgm_source must be 'source', 'new', or 'none'")

    explicit_silent = chk.get("explicit_silent") is True
    has_captions = chk.get("has_captions", True)
    if has_captions is False and not explicit_silent:
        errs.append(
            "assemble-check.json: has_captions=false forbidden unless explicit_silent=true"
        )
    elif has_captions:
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
        else:
            try:
                cues_data = json.loads((film / cues_rel).read_text())
                if not isinstance(cues_data, list) or len(cues_data) == 0:
                    errs.append(f"assemble-check.json: caption_cues_path has zero cues: {cues_rel}")
            except Exception as e:
                errs.append(f"assemble-check.json: invalid caption_cues_path JSON ({e})")

    if chk.get("leading_glitch_trimmed") is not True:
        errs.append(
            "assemble-check.json: leading_glitch_trimmed must be true "
            "(run audio glitch trim before concat)"
        )
    return errs
