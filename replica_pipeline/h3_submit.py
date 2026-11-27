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
    from .script_lint import lint_prompt, load_brief_rules
    from .video_qa import extract_shot_contact_sheet
    from .voice_lock import (
        ensure_voice_lock,
        first_shot_id,
        voice_lock_path,
        voice_meta_path,
    )
except ImportError:  # flat `_pipeline/` on disk
    from candidates import archive_current_shot
    from gate import require as pipeline_require
    from script_lint import lint_prompt, load_brief_rules
    from video_qa import extract_shot_contact_sheet
    from voice_lock import (
        ensure_voice_lock,
        first_shot_id,
        voice_lock_path,
        voice_meta_path,
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


def inject_audio_tag(prompt: str) -> str:
    """Ensure prompt references <Audio 1> for voice-timbre lock."""
    if "<Audio 1>" in prompt:
        return prompt
    tag = (
        "<Audio 1> is the voice-timbre lock for (S1) across the whole film; "
        "keep the same speaker identity and room tone.\n"
    )
    # Prefer insert after Picture line if present
    lines = prompt.splitlines(keepends=True)
    out = []
    inserted = False
    for i, line in enumerate(lines):
        out.append(line)
        if not inserted and "<Picture 1>" in line:
            out.append(tag if tag.endswith("\n") else tag + "\n")
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
    s01 = first_shot_id(film)
    frame_b64 = base64.b64encode(frame.read_bytes()).decode()

    if shot == s01:
        payload = {
            "mode": "i2v",
            "prompt": prompt,
            "width": 576,
            "height": 1024,
            "durationSeconds": duration,
            "seed": seed,
            "firstFrameBase64": frame_b64,
        }
        meta = {"mode": "i2v", "voice_ref": None, "size_still": str(frame)}
        return payload, meta

    # S02+: require voice lock from S01
    vpath = voice_lock_path(film)
    if not vpath.is_file():
        raise SystemExit(
            f"GATE FAIL: missing {vpath} — finish S01 download then "
            f"`python3 _pipeline/pipeline.py extract-voice {film}`"
        )
    prompt2 = inject_audio_tag(prompt)
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
        "voice_ref": str(vpath),
        "voice_sha256": hashlib.sha256(vpath.read_bytes()).hexdigest(),
        "size_still": str(frame),
        "prompt_injected_audio_tag": prompt2 != prompt,
    }
    return payload, meta


def _planned_shot_sec(film: Path, shot: str) -> float | None:
    plan_p = film / "qa" / "REBEAT-PLAN.json"
    if not plan_p.is_file():
        return None
    try:
        for s in json.loads(plan_p.read_text()).get("shots") or []:
            if s.get("id") == shot and s.get("sec") is not None:
                return float(s["sec"])
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
    elif plan_sec is not None and abs(float(duration) - plan_sec) > 2.0:
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

    assert frame.is_file(), frame
    prompt = prompt_path.read_text()
    assert prompt.strip()

    if reroll and (video.is_file() or taskf.is_file()):
        archived = archive_current_shot(
            film,
            shot,
            note="archived before reroll",
            clear_active=True,
        )
        if archived:
            log(shot, "ARCHIVED", archived["candidate_id"], archived.get("visual_status"))

    s01 = first_shot_id(film)
    lint = lint_prompt(
        prompt,
        shot_id=shot,
        shot_sec=float(duration),
        is_first_shot=(shot == s01),
        rules=load_brief_rules(film),
    )
    for w in lint["warnings"]:
        log(shot, "PROMPT_WARN", w)
    if not lint["ok"]:
        raise SystemExit("PROMPT LINT FAIL:\n  - " + "\n  - ".join(lint["errors"]))

    key = api_key()
    state = json.loads(taskf.read_text()) if taskf.exists() else {}

    def save() -> None:
        tmp = taskf.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(taskf)

    active_task_guard(film / "tasks", taskf)

    if state.get("task_id"):
        # Fail closed if still, prompt, or S01 voice-lock wav changed since task_id was issued.
        cur_frame_sha = hashlib.sha256(frame.read_bytes()).hexdigest()
        s01 = first_shot_id(film)
        effective_prompt = prompt if shot == s01 else inject_audio_tag(prompt)
        cur_prompt_sha = hashlib.sha256(effective_prompt.encode()).hexdigest()
        stale_reasons = []
        if state.get("frame_sha256") and state["frame_sha256"] != cur_frame_sha:
            stale_reasons.append("first-frame still")
        if state.get("prompt_sha256") and state["prompt_sha256"] != cur_prompt_sha:
            stale_reasons.append("prompt")
        if shot != s01 and state.get("voice_sha256"):
            vpath = voice_lock_path(film)
            if vpath.is_file():
                cur_voice_sha = hashlib.sha256(vpath.read_bytes()).hexdigest()
                if state["voice_sha256"] != cur_voice_sha:
                    stale_reasons.append("S01-voice.wav")
        if stale_reasons:
            raise SystemExit(
                f"STALE TASK for {shot}: {', '.join(stale_reasons)} changed since submission "
                f"(task_id={state['task_id']}). Archive/remove {taskf} before resubmitting."
            )

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
            "prompt_sha256": hashlib.sha256(
                (payload["prompt"]).encode()
            ).hexdigest(),
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
                info = json.loads(
                    subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "json",
                            str(video),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        str(video),
                        "-map",
                        "0",
                        "-f",
                        "null",
                        "-",
                    ],
                    check=True,
                )
                state.update(
                    state="DOWNLOADED",
                    file=str(video),
                    sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                    duration=info.get("format", {}).get("duration"),
                    technical_decode="PASS",
                )
                save()
                log(shot, "DOWNLOADED", state["duration"], video)
                break
            time.sleep(12)
        else:
            raise SystemExit("Timeout waiting for completion")

    # After S01 lands, refresh the film voice lock for later shots (fail-closed if broken)
    if shot == first_shot_id(film):
        meta = ensure_voice_lock(film, force=True)
        log(shot, "VOICE_LOCK", meta.get("wav"), f"{meta.get('duration_sec')}s")

    # Extract 4-frame contact sheet for mandatory visual QA
    try:
        sheet = extract_shot_contact_sheet(film, shot)
        log(shot, "CONTACT_SHEET", sheet)
    except Exception as e:
        log(shot, "CONTACT_SHEET_WARN", type(e).__name__, str(e)[:160])

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
    sys.exit(main())
