#!/usr/bin/env python3
"""Gated H3 submit with film-level voice lock + still size lock.

S01: i2v + firstFrameBase64 (opens the voice bank).
S02+: ref2v + referenceImagesBase64=[still] + referenceAudiosBase64=[S01 voice].
        Local H3 rejects i2v+referenceAudios (422); ref2v accepts image+audio.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .candidates import archive_current_shot
    from .gate import require as pipeline_require
    from .script_lint import ROLE_RE, lint_prompt, load_brief_rules, project_dual_text, read_strict_json
    from .video_qa import extract_shot_contact_sheet, ffprobe_duration, file_sha256, has_audio_stream, scan_audio_glitches
    from .voice_lock import (
        ensure_voice_lock,
        first_shot_id,
        voice_requirements,
    )
except ImportError:  # flat `_pipeline/` on disk
    from candidates import archive_current_shot
    from gate import require as pipeline_require
    from script_lint import ROLE_RE, lint_prompt, load_brief_rules, project_dual_text, read_strict_json
    from video_qa import extract_shot_contact_sheet, ffprobe_duration, file_sha256, has_audio_stream, scan_audio_glitches
    from voice_lock import (
        ensure_voice_lock,
        first_shot_id,
        voice_requirements,
    )

BASE = "http://10.10.254.110:28000"


def base_url() -> str:
    return (os.environ.get("H3_BASE_URL") or BASE).rstrip("/")


def log(shot: str, *a) -> None:
    print(time.strftime("%H:%M:%S"), shot, *a, flush=True)


def api_key() -> str:
    env_key = os.environ.get("H3_API_KEY", "").strip()
    if env_key:
        return env_key
    return subprocess.check_output(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "video_remix.api",
            "-a",
            "H3_API_KEY",
            "-w",
        ],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def req(key: str, path: str, method: str = "GET", body=None, timeout: int = 120):
    headers = {"X-API-Key": key}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode()
    r = urllib.request.Request(base_url() + path, headers=headers, data=data, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as res:
        return res.read()


def inject_audio_tag(prompt: str, *, role: str | None = None) -> str:
    """Ensure prompt references <Audio 1> for voice-timbre lock."""
    if "<Audio 1>" in prompt:
        return prompt
    speaker = f"the {role} speaker" if role and role != "HOST" else "(S1)"
    tag = (
        f"<Audio 1> is the voice-timbre lock for {speaker} across the whole film; "
        "keep the same speaker identity and room tone.\n"
    )
    # Prefer insert after Picture line if present
    lines = prompt.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and "<Picture 1>" in line:
            out.append(("" if line.endswith("\n") else "\n") + tag)
            inserted = True
    if not inserted:
        out.insert(0, tag if tag.endswith("\n") else tag + "\n")
    return "".join(out)


def active_task_guard(tasks_dir: Path, taskf: Path) -> None:
    terminal = {
        "DOWNLOADED",
        "FAILED",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "REJECTED",
        "ASR_SCREENED",
        "NEEDS_ACCEPTANCE",
    }
    ok_done = {"COMPLETED", "SUCCEEDED", "SUCCESS", "DOWNLOADED"} | terminal
    for other in tasks_dir.glob("*.task.json"):
        if other == taskf:
            continue
        o = json.loads(other.read_text())
        st = o.get("state", "")
        if st and st not in terminal and o.get("task_id"):
            if st.upper() not in ok_done:
                raise SystemExit(f"Active task exists: {other.name} state={st}")


def build_payload(
    film: Path,
    shot: str,
    prompt: str,
    frame: Path,
    duration: float,
    seed: int = 83,
) -> tuple[dict, dict]:
    """Return (payload, meta)."""
    film = Path(film)
    role, _, vpath = voice_requirements(film, shot, strict=False, prompt=prompt)
    raw_sha = hashlib.sha256(prompt.encode()).hexdigest()
    prompt = ROLE_RE.sub("", project_dual_text(prompt))
    frame_b64 = base64.b64encode(frame.read_bytes()).decode()

    if vpath is None:
        payload = {
            "mode": "i2v",
            "prompt": prompt,
            "width": 576,
            "height": 1024,
            "durationSeconds": duration,
            "seed": seed,
            "firstFrameBase64": frame_b64,
        }
        meta = {"mode": "i2v", "voice_ref": None, "size_still": str(frame), "role": role,
                "raw_prompt_sha256": raw_sha, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        return payload, meta

    prompt2 = inject_audio_tag(prompt, role=role)
    payload = {
        "mode": "ref2v",
        "prompt": prompt2,
        "width": 576,
        "height": 1024,
        "durationSeconds": duration,
        "seed": seed,
        "referenceImagesBase64": [frame_b64],
        "referenceAudiosBase64": [base64.b64encode(vpath.read_bytes()).decode()],
        "referenceImageSize": "match",
    }
    meta = {
        "mode": "ref2v",
        "role": role,
        "raw_prompt_sha256": raw_sha,
        "prompt_sha256": hashlib.sha256(prompt2.encode()).hexdigest(),
        "voice_ref": str(vpath),
        "voice_sha256": hashlib.sha256(vpath.read_bytes()).hexdigest(),
        "size_still": str(frame),
        "prompt_injected_audio_tag": prompt2 != prompt,
    }
    return payload, meta


def _save_task(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _validate_task_inputs(film: Path, shot: str, state: dict) -> None:
    prompt_path = film / "prompts" / f"{shot}.txt"
    frame = film / "first-frames/h3" / f"{shot}-first.jpg"
    prompt = ROLE_RE.sub("", project_dual_text(prompt_path.read_text()))
    if state.get("mode") == "ref2v":
        role, _, wav = voice_requirements(film, shot)
        prompt = inject_audio_tag(prompt, role=role)
        if wav is None or state.get("voice_sha256") != file_sha256(wav):
            raise ValueError(f"{shot}: voice reference changed since submission")
    if (state.get("frame_sha256") and state["frame_sha256"] != file_sha256(frame)
            or state.get("raw_prompt_sha256") and state["raw_prompt_sha256"] != file_sha256(prompt_path)
            or state.get("prompt_sha256") and state["prompt_sha256"] != hashlib.sha256(prompt.encode()).hexdigest()):
        raise ValueError(f"{shot}: task prompt or still changed since submission; archive before reroll")


def accept_downloaded_shot(film: Path, shot: str, *, vlm_provider: str = "none", asr_path=None) -> dict:
    try:
        from .asr_verify import verify_shot_meaning
        from .vlm_review import review_video_with_vlm
    except ImportError:
        from asr_verify import verify_shot_meaning
        from vlm_review import review_video_with_vlm
    film = Path(film).resolve()
    pipeline_require(film, "h3_submit", shot=shot)
    role, bootstrap, _ = voice_requirements(film, shot)
    if vlm_provider not in {"none", "auto", "codex", "openai"}:
        raise ValueError("invalid video VLM provider")
    video = film / "shots" / f"{shot}.mp4"
    task = film / "tasks" / f"{shot}.task.json"
    state = read_strict_json(task) if task.is_file() else {}
    state.update(state="NEEDS_ACCEPTANCE", shot=shot)
    _save_task(task, state)
    try:
        _validate_task_inputs(film, shot, state)
        digest = file_sha256(video)
        if state.get("download_sha256", state.get("sha256", digest)) != digest:
            raise ValueError(f"{shot}: downloaded video changed; select a tracked candidate or reroll")
        duration = ffprobe_duration(video)
        planned = _planned_shot_sec(film, shot) or state.get("duration")
        if not math.isfinite(duration) or not 0 < duration or (planned and abs(duration - float(planned)) > 0.5):
            raise ValueError(f"{shot}: decoded duration {duration}s disagrees with {planned}s")
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(video), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], check=True)
        audio = has_audio_stream(video)
        if not role and asr_path is None and not audio:
            empty = film / "qa/asr" / f"{shot}.json"
            empty.parent.mkdir(parents=True, exist_ok=True)
            empty.write_text(json.dumps({"segments": [], "video_sha256": digest}) + "\n")
        meaning = verify_shot_meaning(film, shot, asr_path=asr_path)
        if meaning["verdict"] != "PASS":
            raise ValueError(f"{shot}: ASR meaning failed: {'; '.join(meaning['issues'])}")
        glitches = scan_audio_glitches(video) if audio else {}
        if role and (not glitches.get("segments") or glitches.get("mid_speech_glitches") or
                     any(pause["duration"] >= 1.5 for pause in glitches.get("long_pauses", []))):
            raise ValueError(f"{shot}: missing audio or unrecoverable mid-speech glitches")
        sheet = extract_shot_contact_sheet(film, shot)
        if not sheet.is_file():
            raise ValueError(f"{shot}: contact sheet was not generated")
        if vlm_provider != "none" and review_video_with_vlm(film, shot, provider=vlm_provider)["status"] != "PASS":
            raise ValueError(f"{shot}: VLM video review failed")
        if file_sha256(video) != digest:
            raise ValueError(f"{shot}: video changed during acceptance")
        voice = ensure_voice_lock(film, role=role, shot=shot) if role and shot == bootstrap else None
        state.pop("acceptance_error", None)
        state.update(state="DOWNLOADED", sha256=digest, duration=duration, role=role,
                     technical_decode="PASS", acceptance="PASS", contact_sheet_sha256=file_sha256(sheet),
                     acceptance_asr_sha256=meaning["asr_sha256"])
        _save_task(task, state)
        return {"ok": True, "shot": shot, "meaning": meaning, "contact_sheet": str(sheet), "voice": voice}
    except Exception as exc:
        state.update(state="NEEDS_ACCEPTANCE", acceptance="FAIL", acceptance_error=str(exc))
        _save_task(task, state)
        raise


def _planned_shot_sec(film: Path, shot: str) -> float | None:
    plan_p = film / "qa" / "REBEAT-PLAN.json"
    if not plan_p.is_file():
        return None
    try:
        for s in json.loads(plan_p.read_text()).get("shots") or []:
            if s.get("id") == shot and s.get("sec") is not None:
                return float(s.get("audio_source_sec", s["sec"]))
    except Exception:
        return None
    return None


def run(
    film: Path,
    shot: str,
    duration: float | None = None,
    seed: int = 83,
    *,
    reroll: bool = False,
) -> int:
    film = Path(film)
    pipeline_require(film, "h3_submit", shot=shot)

    plan_sec = _planned_shot_sec(film, shot)
    if duration is None:
        duration = plan_sec if plan_sec is not None else 10.0
    elif plan_sec is not None and abs(float(duration) - plan_sec) > 0.5:
        raise SystemExit(
            f"GATE FAIL: {shot} requested duration={duration}s conflicts with "
            f"REBEAT-PLAN.json sec={plan_sec}s"
        )

    # Size canon must exist (written at stills stage)
    size_lock = film / "qa" / "SIZE-LOCK.json"
    if not size_lock.is_file():
        raise SystemExit(
            f"GATE FAIL: missing {size_lock} — stills stage must lock film-level product size"
        )

    frame = film / "first-frames" / "h3" / f"{shot}-first.jpg"
    prompt_path = film / "prompts" / f"{shot}.txt"
    taskf = film / "tasks" / f"{shot}.task.json"
    video = film / "shots" / f"{shot}.mp4"
    taskf.parent.mkdir(parents=True, exist_ok=True)
    video.parent.mkdir(parents=True, exist_ok=True)

    if not frame.is_file():
        raise FileNotFoundError(frame)
    prompt = prompt_path.read_text()
    if not prompt.strip():
        raise ValueError(f"empty prompt {prompt_path}")

    if reroll and (video.is_file() or taskf.is_file()):
        previous = read_strict_json(taskf) if taskf.is_file() else {}
        if not previous.get("task_id") and previous.get("state") in {"SUBMITTING", "UNCERTAIN_SUBMIT"}:
            raise ValueError(f"{shot}: recover uncertain submission before rerolling")
        archived = archive_current_shot(
            film,
            shot,
            note="archived before reroll",
            clear_active=True,
        )
        if archived:
            log(shot, "ARCHIVED", archived["candidate_id"], archived.get("visual_status"))

    s01 = first_shot_id(film)
    role, bootstrap, _ = voice_requirements(film, shot)
    lint = lint_prompt(
        prompt,
        shot_id=shot,
        shot_sec=float(duration),
        is_first_shot=(shot == s01 or shot == bootstrap),
        rules=load_brief_rules(film),
        silent=not role,
    )
    for w in lint["warnings"]:
        log(shot, "PROMPT_WARN", w)
    if not lint["ok"]:
        raise SystemExit("PROMPT LINT FAIL:\n  - " + "\n  - ".join(lint["errors"]))

    state = read_strict_json(taskf) if taskf.is_file() else {}

    def save() -> None:
        _save_task(taskf, state)

    if state.get("task_id"):
        _validate_task_inputs(film, shot, state)
    if video.is_file() and state.get("state") in {"NEEDS_ACCEPTANCE", "DOWNLOADED", "COMPLETED", "SUCCEEDED", "SUCCESS"}:
        accept_downloaded_shot(film, shot)
        return 0
    if video.is_file() and not state.get("task_id"):
        raise ValueError(f"{shot}: existing untracked video; run accept-shot or use --reroll")
    active_task_guard(film / "tasks", taskf)
    key = api_key()

    if not state.get("task_id"):
        if state.get("state") in {"SUBMITTING", "UNCERTAIN_SUBMIT"}:
            raise SystemExit("Submission uncertain; do not repost")
        payload, meta = build_payload(film, shot, prompt, frame, duration, seed=seed)
        # Drop huge base64 from saved state
        state = {
            "state": "SUBMITTING",
            "shot": shot,
            "mode": meta["mode"],
            "duration": duration,
            "frame": str(frame),
            "frame_sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
            "prompt_sha256": meta["prompt_sha256"],
            "raw_prompt_sha256": file_sha256(prompt_path),
            "role": meta["role"],
            "voice_ref": meta.get("voice_ref"),
            "voice_sha256": meta.get("voice_sha256"),
            "size_lock": str(size_lock),
            "task_id": None,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        save()
        try:
            response = json.loads(req(key, "/v1/videos", "POST", payload, timeout=180))
        except urllib.error.HTTPError as e:
            err = e.read(2000).decode("utf-8", "replace")
            state.update(
                state="REJECTED" if 400 <= e.code < 500 else "UNCERTAIN_SUBMIT",
                error=f"HTTP {e.code}: {err[:500]}",
            )
            save()
            raise SystemExit(state["error"])
        except Exception as e:
            state.update(state="UNCERTAIN_SUBMIT", error=f"{type(e).__name__}: {e}")
            save()
            raise
        obj = response.get("data") or response
        tid = obj.get("taskId") or obj.get("task_id") or obj.get("id")
        if not tid:
            state.update(state="UNCERTAIN_SUBMIT", error="no taskId", response=response)
            save()
            raise SystemExit("No taskId; no repeat POST")
        state.update(state="SUBMITTED", task_id=str(tid), raw_submit={"taskId": tid})
        save()
        log(shot, "SUBMITTED", tid, meta["mode"])

    if not (state.get("state") == "DOWNLOADED" and video.is_file()):
        deadline = time.monotonic() + 3600
        last = None
        while time.monotonic() < deadline:
            try:
                raw = json.loads(req(key, "/v1/videos/" + state["task_id"], timeout=40))
                obj = raw.get("data") or raw
                status = str(obj.get("status", "")).upper()
            except Exception as e:
                log(shot, "POLL_ERROR", type(e).__name__, str(e)[:120])
                time.sleep(15)
                continue
            if status != last:
                log(shot, "STATUS", status)
                last = status
            state["state"] = status
            state["last_polled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save()
            if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
                state["terminal_error"] = obj
                save()
                raise SystemExit(
                    f"Remote failure: {json.dumps(obj, ensure_ascii=False)[:700]}"
                )
            if status in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
                part = video.with_suffix(".part")
                part.write_bytes(
                    req(key, "/v1/videos/" + state["task_id"] + "/file", timeout=300)
                )
                part.replace(video)
                state.update(state="NEEDS_ACCEPTANCE", file=str(video), download_sha256=file_sha256(video))
                save()
                break
            time.sleep(12)
        else:
            raise SystemExit("Timeout waiting for completion")

    accept_downloaded_shot(film, shot)
    state = read_strict_json(taskf)

    # Snapshot completed attempt into shots/candidates/ so later rerolls never lose it
    if video.is_file():
        try:
            cand = archive_current_shot(film, shot, note="h3 completed download", clear_active=False)
            if cand:
                log(shot, "CANDIDATE", cand["candidate_id"])
        except Exception as e:
            log(shot, "CANDIDATE_WARN", type(e).__name__, str(e)[:160])

    print(
        json.dumps(
            {
                "ok": True,
                "shot": shot,
                "task_id": state["task_id"],
                "file": str(video),
                "mode": state.get("mode"),
                "voice_ref": state.get("voice_ref"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reroll = False
    if "--reroll" in argv:
        reroll = True
        argv.remove("--reroll")
    if len(argv) < 2:
        print(
            "Usage: h3_submit.py FILM_DIR SHOT [DURATION] [SEED] [--reroll]",
            file=sys.stderr,
        )
        return 3
    film = Path(argv[0])
    shot = argv[1]
    duration = float(argv[2]) if len(argv) > 2 else None
    seed = int(argv[3]) if len(argv) > 3 else 83
    return run(film, shot, duration, seed=seed, reroll=reroll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
