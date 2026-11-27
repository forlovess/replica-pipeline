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
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    from .gate import require as pipeline_require
    from .script_lint import extract_dialogue_segments, project_dual_text
    from .video_qa import (
        audio_db_bins,
        ffprobe_duration,
        find_active_segments,
        scan_audio_glitches,
    )
except ImportError:
    from gate import require as pipeline_require
    from script_lint import extract_dialogue_segments, project_dual_text
    from video_qa import (
        audio_db_bins,
        ffprobe_duration,
        find_active_segments,
        scan_audio_glitches,
    )

PUNCTUATION = set("，。、！？,.!? ")
TRAILING_CAPTION_PUNCT = "。，、！？,.!?…;； "
CLAUSE_SPLIT_RE = re.compile(r"[，。！？；,.!?\n]+")


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
) -> str:
    """Build ffmpeg filter_complex normalizing speech (-16 LUFS) and ducking BGM underneath."""
    total_dur = max(0.5, float(total_dur))
    fi = min(float(fade_in), total_dur * 0.35)
    fo = min(float(fade_out), total_dur * 0.45)
    fade_out_st = max(0.0, total_dur - fo)
    return (
        "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,asplit=2[vo][vo_sc];"
        f"[1:a]atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
        f"aresample=48000,aformat=channel_layouts=stereo,"
        f"volume={bgm_db:.1f}dB,"
        f"afade=t=in:st=0:d={fi:.2f},"
        f"afade=t=out:st={fade_out_st:.3f}:d={fo:.2f}[bgm_raw];"
        "[bgm_raw][vo_sc]sidechaincompress="
        "threshold=0.04:ratio=5:attack=40:release=350:makeup=1[bgm_ducked];"
        "[vo][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=0.25[aout]"
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
        shutil.copy2(video_path, out_path)
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


def _default_phrases_for_shot(film: Path, shot_obj: dict[str, Any]) -> list[str]:
    if shot_obj.get("phrases"):
        return [str(p).strip() for p in shot_obj["phrases"] if str(p).strip()]
    sid = shot_obj["id"]
    pfile = film / "prompts" / f"{sid}.txt"
    spoken = ""
    if pfile.is_file():
        segs = extract_dialogue_segments(pfile.read_text())
        spoken = " ".join(segs)
    else:
        spoken = str(shot_obj.get("spoken") or shot_obj.get("vo") or "").strip()
    if not spoken:
        return []
    parts = [p.strip() for p in CLAUSE_SPLIT_RE.split(spoken.replace("||", "\n")) if p.strip()]
    return parts


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


def _encode_trimmed_clip(src: Path, dest: Path, t0: float, t1: float, *, width: int, height: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fade_out_st = max(t0, t1 - 0.03)
    has_audio = _has_audio_stream(src)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    if has_audio:
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
) -> dict[str, Any]:
    """Trim glitches, concatenate shots, duck BGM, align ASR cues, and emit final deliverables."""
    film = Path(film)
    if not skip_gate:
        pipeline_require(film, "assemble")

    plan_path = film / "qa" / "REBEAT-PLAN.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"missing {plan_path}")
    shots = json.loads(plan_path.read_text()).get("shots") or []
    if not shots:
        raise ValueError("REBEAT-PLAN.json has no shots")

    work = film / "qa" / "assemble-work"
    if work.exists():
        shutil.rmtree(work)
    clips_dir = work / "clips"
    clips_dir.mkdir(parents=True)

    trim_jobs: list[tuple[dict[str, Any], Path, Path, list[dict[str, Any]], dict[str, Any], list[str]]] = []
    for s in shots:
        sid = s["id"]
        src = film / "shots" / f"{sid}.mp4"
        if not src.is_file():
            raise FileNotFoundError(f"missing shot video: {src}")
        asr_cand = (Path(asr_dir) / f"{sid}.json") if asr_dir else (film / "qa" / "asr" / f"{sid}.json")
        phrases = _default_phrases_for_shot(film, s)
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
        chars = asr_chars(asr_cand) if asr_cand.is_file() else []
        if _has_audio_stream(src):
            win = detect_trim_window(src, chars)
        else:
            sdur = round(ffprobe_duration(src), 3)
            win = {"t0": 0.0, "t1": sdur, "skip_head_chars": 0, "leading_glitch": None, "duration": sdur}
        trim_jobs.append((s, src, clips_dir / f"{sid}.mp4", chars, win, phrases))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [
            pool.submit(_encode_trimmed_clip, src, dest, win["t0"], win["t1"], width=width, height=height)
            for _, src, dest, _, win, _ in trim_jobs
        ]
        for fut in futs:
            fut.result()

    concat_list = work / "concat.txt"
    concat_list.write_text("\n".join(f"file '{dest.resolve()}'" for _, _, dest, _, _, _ in trim_jobs) + "\n")
    raw_concat = work / "concat.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(raw_concat)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    strict_bgm = film / "qa" / "strict-with-bgm.mp4"
    bgm_file = Path(bgm_path) if bgm_path else next((film / "qa").glob("bgm.*"), None)
    eff_bgm_source = bgm_source if bgm_source in {"source", "new", "none"} else "none"
    has_spoken = any(phrases for _, _, _, _, _, phrases in trim_jobs)
    eff_bgm_db = float(bgm_db) if bgm_db is not None else (-14.0 if has_spoken else -4.0)
    if bgm_file and bgm_file.is_file():
        mix_audio_with_ducking(raw_concat, bgm_file, strict_bgm, bgm_db=eff_bgm_db)
        if eff_bgm_source == "none":
            eff_bgm_source = "source"
    else:
        shutil.copy2(raw_concat, strict_bgm)

    cues: list[dict[str, Any]] = []
    offset = 0.0
    for s, src, dest, chars, win, phrases in trim_jobs:
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

    if not cues and not allow_uncaptioned:
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
        shutil.copy2(strict_bgm, captioned)
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
    }
    (film / "qa" / "assemble-check.json").write_text(json.dumps(chk, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "film": film.name, "captioned": str(captioned), "assemble_check": chk}


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
