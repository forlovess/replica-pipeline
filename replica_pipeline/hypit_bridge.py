"""Hypit CLI & SVML project bridge (optional enhancement, zero hard runtime lock-in).

Connects replica-pipeline to installed Hypit tools:
- `prepare_source`: probes source media, extracts visual scene boundaries into
  `qa/cuts.json`, generates `qa/source-overview.jpg`, runs optional `hypit transcribe`,
  and emits `qa/source.json` using Hypit CLI when enabled or local ffmpeg fallback.
- `export_hypit_project`: compiles a film's REBEAT-PLAN, dialogue, shots, captions,
  and BGM into a valid editable Hypit SVML/SVRun workspace (`production.svml` +
  `render.svrun` + `package.json`) ready for `hypit plan` / `hypit studio`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

try:
    from .audio_stem import extract_source_stems
    from .script_lint import DUAL_TEXT_RE, extract_dialogue_segments, project_dual_text
    from .video_qa import ffprobe_duration
except ImportError:
    from audio_stem import extract_source_stems
    from script_lint import DUAL_TEXT_RE, extract_dialogue_segments, project_dual_text
    from video_qa import ffprobe_duration


def has_hypit() -> bool:
    return shutil.which("hypit") is not None


def _run_json(cmd: list[str], *, timeout: int = 120) -> Any:
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, timeout=timeout)
    return json.loads(out)


def probe_source_video(source: Path, *, use_hypit: bool = False) -> dict[str, Any]:
    """Probe duration, width, height, and fps via Hypit CLI or ffprobe."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"missing source video: {source}")
    if use_hypit and has_hypit():
        try:
            raw = _run_json(["hypit", "media", "probe", str(source), "--json"], timeout=30)
            return {"engine": "hypit", "duration": float(raw.get("duration") or ffprobe_duration(source)), "raw": raw}
        except Exception:
            pass

    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(source),
        ],
        text=True,
    )
    info = json.loads(out)
    vstream = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), {})
    astream = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), None)
    return {
        "engine": "ffprobe",
        "duration": round(float((info.get("format") or {}).get("duration") or 0.0), 3),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "has_audio": astream is not None,
    }


def detect_source_boundaries(
    source: Path,
    *,
    threshold: float = 0.35,
    use_hypit: bool = False,
) -> list[dict[str, Any]]:
    """Detect visual cut timestamps via `hypit media boundaries` or ffmpeg scene detection."""
    source = Path(source)
    if use_hypit and has_hypit():
        try:
            raw = _run_json(
                ["hypit", "media", "boundaries", str(source), "--threshold", str(threshold), "--json"],
                timeout=120,
            )
            cands = raw.get("boundaries") or raw.get("candidates") or []
            if isinstance(cands, list):
                return [
                    {"t": round(float(c.get("time") or c.get("t") or 0.0), 3), "score": float(c.get("score") or 0.0)}
                    for c in cands
                ]
        except Exception:
            pass

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(source),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    cuts: list[dict[str, Any]] = []
    for line in proc.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cuts.append({"t": round(float(m.group(1)), 3), "score": threshold})
    return cuts


def build_source_overview(
    source: Path,
    out_jpg: Path,
    *,
    duration: float,
    transcript_json: Path | None = None,
    use_hypit: bool = False,
    columns: int = 4,
) -> Path:
    """Create a grid overview of the source film (using `hypit media tile` when requested)."""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    if use_hypit and has_hypit():
        try:
            cmd = [
                "hypit",
                "media",
                "tile",
                str(source),
                "--start",
                "0",
                "--end",
                f"{duration:.2f}",
                "--frames",
                "12",
                "--columns",
                str(columns),
                "--cell",
                "320",
                "--to",
                str(out_jpg),
            ]
            if transcript_json and transcript_json.is_file():
                cmd.extend(["--transcript", str(transcript_json)])
            if out_jpg.exists():
                out_jpg.unlink()
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return out_jpg
        except Exception:
            pass

    step = max(0.4, duration / 12.0)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{step:.3f},scale=320:-2,tile={columns}x3",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out_jpg),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_jpg


def prepare_source(
    film: Path,
    source: Path,
    *,
    language: str = "zh",
    threshold: float = 0.35,
    use_hypit: bool = False,
    transcribe: bool = False,
    extract_bgm: bool = False,
    bgm_engine: str = "auto",
) -> dict[str, Any]:
    """Run stage-1 source preparation: probe, optional stem split, cuts, ASR, overview tile."""
    film = Path(film)
    source = Path(source)
    qa_dir = film / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)

    probe = probe_source_video(source, use_hypit=use_hypit)
    dur = float(probe.get("duration") or 0.0)
    cuts = detect_source_boundaries(source, threshold=threshold, use_hypit=use_hypit)

    stem_info: dict[str, Any] | None = None
    transcribe_input = source
    if extract_bgm:
        stem_info = extract_source_stems(film, source, engine=bgm_engine)
        voc_rel = stem_info.get("vocals")
        if voc_rel and (film / voc_rel).is_file():
            transcribe_input = film / voc_rel

    cuts_path = qa_dir / "cuts.json"
    cuts_payload = {
        "source": str(source),
        "duration": dur,
        "threshold": threshold,
        "cuts": cuts,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    cuts_path.write_text(json.dumps(cuts_payload, ensure_ascii=False, indent=2) + "\n")

    asr_path = qa_dir / "source-asr.json"
    asr_chars = 0
    if transcribe and has_hypit():
        subprocess.run(
            [
                "hypit",
                "transcribe",
                str(transcribe_input),
                "--language",
                language,
                "--to",
                str(asr_path),
            ],
            check=True,
        )
    if asr_path.is_file():
        try:
            body = json.loads(asr_path.read_text())
            asr_chars = sum(len(w.get("word") or w.get("text") or "") for s in body.get("segments") or [] for w in s.get("words") or [])
        except Exception:
            asr_chars = 0

    overview_path = qa_dir / "source-overview.jpg"
    build_source_overview(
        source,
        overview_path,
        duration=dur,
        transcript_json=asr_path if asr_path.is_file() else None,
        use_hypit=use_hypit,
    )

    src_json = qa_dir / "source.json"
    src_payload: dict[str, Any] = {
        "path": str(source),
        "duration": dur,
        "width": probe.get("width"),
        "height": probe.get("height"),
        "cuts_count": len(cuts),
        "cuts_path": "qa/cuts.json",
        "overview_path": "qa/source-overview.jpg",
        "asr_path": "qa/source-asr.json" if asr_path.is_file() else "",
        "asr_chars": asr_chars,
        "engine": "hypit" if (use_hypit and has_hypit()) else "ffmpeg",
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if stem_info is not None:
        src_payload["stem_split"] = {
            "path": "qa/STEM-SPLIT.json",
            "usable_bgm": stem_info.get("usable_bgm"),
            "bgm": stem_info.get("bgm"),
            "vocals": stem_info.get("vocals"),
        }
    src_json.write_text(json.dumps(src_payload, ensure_ascii=False, indent=2) + "\n")
    return src_payload


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return s or "film"


def _shot_script_body(film: Path, shot: dict[str, Any]) -> str:
    sid = shot["id"]
    pfile = film / "prompts" / f"{sid}.txt"
    if pfile.is_file():
        segs = extract_dialogue_segments(pfile.read_text())
        if segs:
            return " || ".join(xml_escape(s) for s in segs)
    spoken = str(shot.get("spoken") or shot.get("vo") or "").strip()
    if spoken:
        return xml_escape(spoken)
    return ""


def _rel_pos(target: Path, base: Path) -> str:
    return "./" + Path(os.path.relpath(target.resolve(), base.resolve())).as_posix()


def export_hypit_project(
    film: Path,
    *,
    out_dir: Path | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> dict[str, Any]:
    """Export the film into a valid Hypit project (`package.json`, `production.svml`, `render.svrun`)."""
    film = Path(film)
    plan_path = film / "qa" / "REBEAT-PLAN.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"REBEAT-PLAN.json required for Hypit export: {plan_path}")
    plan = json.loads(plan_path.read_text())
    shots = plan.get("shots") or []
    if not shots:
        raise ValueError("REBEAT-PLAN.json has no shots")

    dest = Path(out_dir) if out_dir else (film / "hypit-project")
    dest.mkdir(parents=True, exist_ok=True)

    pkg = {
        "name": f"replica-{_slug(film.name)}",
        "private": True,
        "hypit": {"project": True},
    }
    (dest / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")

    script_lines: list[str] = []
    media_lines: list[str] = []
    timeline_windows: list[str] = []
    visual_clips: list[str] = []
    audio_clips: list[str] = []

    prev_anchor = "start"
    last_win = None
    for idx, s in enumerate(shots):
        sid = s["id"]
        seg_name = sid.lower()
        win_id = f"w-{seg_name}"
        body = _shot_script_body(film, s)
        if body:
            script_lines.append(f"    <{seg_name}><HOST>{body}</HOST></{seg_name}>")
        else:
            script_lines.append(f"    <{seg_name}/>")

        shot_mp4 = film / "shots" / f"{sid}.mp4"
        if shot_mp4.is_file():
            src_ref = _rel_pos(shot_mp4, dest)
            media_lines.append(f'  <file id="raw-{seg_name}" from="{xml_escape(src_ref)}"/>')
            media_lines.append(
                f'  <mediaop:Normalize id="norm-{seg_name}" source={{raw-{seg_name}}} clock={{clock}} '
                f'video="primary-moving" audio="default" span-authority="video"/>'
            )
            extent_expr = f"{{norm-{seg_name}.extent}}"
        else:
            sec = s.get("sec") or 6
            extent_expr = f'"{sec}s"'

        from_attr = 'from="start"' if idx == 0 else f'from={{{prev_anchor}}}'
        timeline_windows.append(f'    <time:Window id="{win_id}" {from_attr} for={extent_expr}/>')
        prev_anchor = f"main-timeline.{win_id}.end"
        last_win = win_id

        if shot_mp4.is_file():
            visual_clips.append(
                f'    <visual:Clip id="v-{seg_name}" media={{norm-{seg_name}.media}} '
                f'during={{main-timeline.{win_id}}} frame={{full}} z="10" fit="cover"/>'
            )
            audio_clips.append(
                f'    <audio:Clip id="a-{seg_name}" source={{norm-{seg_name}.media}} '
                f'during={{main-timeline.{win_id}}}/>'
            )

    bgm_file = next((film / "qa").glob("bgm.*"), None) or next((film).glob("bgm.*"), None)
    if bgm_file and bgm_file.is_file():
        bgm_ref = _rel_pos(bgm_file, dest)
        media_lines.append(f'  <file id="raw-bgm" from="{xml_escape(bgm_ref)}"/>')
        audio_clips.append('    <audio:Clip id="bgm-bed" source={raw-bgm} from="start" gain="-14dB"/>')

    end_ref = f"{last_win}.end" if last_win else '"0s"'
    svml = "\n".join(
        [
            '<?svml using="@hypit/markup@1"?>',
            "<svml>",
            '  <import as="audio" from="@hypit/audio-track@1"/>',
            '  <import as="visual" from="@hypit/visual-track@1"/>',
            '  <import from="@hypit/script@1"/>',
            '  <import as="mediaop" from="@hypit/media-operations@1"/>',
            '  <import as="media" from="@hypit/media@1"/>',
            '  <import as="time" from="@hypit/timeline-author@1"/>',
            '  <import as="space" from="@hypit/spatial@1"/>',
            '  <import as="film" from="@hypit/film@1"/>',
            '  <import as="html" from="@hypit/html-video@1"/>',
            "",
            '  <script id="story">',
            *script_lines,
            "  </script>",
            f'  <space:Canvas id="canvas" width="{width}" height="{height}"/>',
            f'  <time:Clock id="clock" frame-rate="{fps}"/>',
            '  <space:Frame id="full" within={canvas.bounds} left="0%" top="0%" right="100%" bottom="100%"/>',
            *media_lines,
            f'  <time:Timeline id="main-timeline" clock={{clock}} end="{end_ref}">',
            *timeline_windows,
            "  </time:Timeline>",
            '  <visual:Track id="main-visual" timeline={main-timeline.timeline}>',
            *visual_clips,
            "  </visual:Track>",
            '  <audio:Track id="main-audio" timeline={main-timeline.timeline}>',
            *audio_clips,
            "  </audio:Track>",
            '  <film:Film id="main" canvas={canvas.canvas} timeline={main-timeline.timeline}>',
            "    <film:Track source={main-visual.visual}/>",
            "    <film:Track source={main-audio.audio}/>",
            "  </film:Film>",
            '  <html:Video id="final" composition={main.composition} timeline={main-timeline.timeline}/>',
            "</svml>",
            "",
        ]
    )
    svrun = "\n".join(
        [
            '<?svml using="@hypit/markup/run@1"?>',
            '<svrun version="1">',
            '  <author source="./production.svml"/>',
            '  <target output="final.video"/>',
            "</svrun>",
            "",
        ]
    )
    svml_path = dest / "production.svml"
    svrun_path = dest / "render.svrun"
    svml_path.write_text(svml)
    svrun_path.write_text(svrun)

    return {
        "ok": True,
        "project_dir": str(dest),
        "svml": str(svml_path),
        "svrun": str(svrun_path),
        "shots_exported": len(shots),
        "hypit_cli": has_hypit(),
    }
