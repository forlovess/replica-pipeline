"""Run Candidate archiving and selection (inspired by Hypit Runs/Builds reuse).

Prevents destructive overwrites when rerolling H3 shots:
- Snapshots shots/Sxx.mp4, tasks/Sxx.task.json, and contact sheets into
  shots/candidates/Sxx/attempt-NNN/ before a reroll.
- Indexes every attempt in qa/CANDIDATES.json with sha256, prompt_sha256,
  visual verdict, and notes.
- Allows selecting/restoring any prior candidate without losing comparison history.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

try:
    from .video_qa import file_sha256
except ImportError:
    from video_qa import file_sha256

CANDIDATES_NAME = "qa/CANDIDATES.json"
CANDIDATES_DIR = "shots/candidates"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def candidates_path(film: Path) -> Path:
    return Path(film) / CANDIDATES_NAME


def load_candidates(film: Path) -> dict[str, Any]:
    p = candidates_path(film)
    if p.is_file():
        return json.loads(p.read_text())
    return {
        "version": 1,
        "rule": (
            "Retains every generated H3 attempt per shot so rerolls never discard "
            "usable comparison candidates."
        ),
        "shots": {},
        "updated": _now(),
    }


def save_candidates(film: Path, data: dict[str, Any]) -> Path:
    p = candidates_path(film)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = _now()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(p)
    return p


def _current_visual_status(film: Path, shot: str, video_sha: str | None) -> str:
    vlock = Path(film) / "qa" / "H3-VISUAL-LOCK.json"
    if not vlock.is_file():
        return "UNREVIEWED"
    try:
        entry = (json.loads(vlock.read_text()).get("shots") or {}).get(shot)
    except Exception:
        return "UNREVIEWED"
    if not isinstance(entry, dict):
        return "UNREVIEWED"
    if video_sha and entry.get("shot_sha256") and entry["shot_sha256"] != video_sha:
        return "STALE"
    return str(entry.get("status") or "UNREVIEWED").upper()


def archive_current_shot(
    film: Path,
    shot: str,
    *,
    note: str = "",
    clear_active: bool = False,
    visual_status: str | None = None,
) -> dict[str, Any] | None:
    """Archive current shots/Sxx.mp4 + task + contact sheet into shots/candidates/Sxx/attempt-NNN/."""
    film = Path(film)
    vid = film / "shots" / f"{shot}.mp4"
    taskf = film / "tasks" / f"{shot}.task.json"
    sheet = film / "qa" / "h3-contact-sheets" / f"{shot}.jpg"

    if not vid.is_file() and not taskf.is_file():
        return None

    data = load_candidates(film)
    shot_rec = data.setdefault("shots", {}).setdefault(shot, {"active": None, "items": []})
    items: list[dict[str, Any]] = shot_rec.setdefault("items", [])

    vid_sha = file_sha256(vid) if vid.is_file() else None
    task_meta: dict[str, Any] = {}
    if taskf.is_file():
        try:
            task_meta = json.loads(taskf.read_text())
        except Exception:
            task_meta = {}

    # Deduplicate identical video+task_id already archived cleanly
    if vid_sha and not clear_active:
        for existing in items:
            if existing.get("video_sha256") == vid_sha and existing.get("task_id") == task_meta.get("task_id"):
                if note and not existing.get("note"):
                    existing["note"] = note
                    save_candidates(film, data)
                return existing

    next_num = (max((int(it.get("attempt") or 0) for it in items), default=0)) + 1
    cand_id = f"{shot}-attempt{next_num:03d}"
    dest_dir = film / CANDIDATES_DIR / shot / f"attempt-{next_num:03d}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    entry: dict[str, Any] = {
        "candidate_id": cand_id,
        "attempt": next_num,
        "shot": shot,
        "video": None,
        "video_sha256": vid_sha,
        "contact_sheet": None,
        "task": None,
        "task_id": task_meta.get("task_id"),
        "mode": task_meta.get("mode"),
        "frame_sha256": task_meta.get("frame_sha256"),
        "prompt_sha256": task_meta.get("prompt_sha256"),
        "duration": task_meta.get("duration"),
        "visual_status": visual_status or _current_visual_status(film, shot, vid_sha),
        "note": note,
        "archived_at": _now(),
    }

    if vid.is_file():
        dest_vid = dest_dir / f"{shot}.mp4"
        shutil.copy2(vid, dest_vid)
        entry["video"] = str(dest_vid.relative_to(film))
    if sheet.is_file():
        dest_sheet = dest_dir / "contact-sheet.jpg"
        shutil.copy2(sheet, dest_sheet)
        entry["contact_sheet"] = str(dest_sheet.relative_to(film))
    if taskf.is_file():
        dest_task = dest_dir / f"{shot}.task.json"
        shutil.copy2(taskf, dest_task)
        entry["task"] = str(dest_task.relative_to(film))

    items.append(entry)
    if clear_active:
        shot_rec["active"] = None
        vid.unlink(missing_ok=True)
        taskf.unlink(missing_ok=True)
        sheet.unlink(missing_ok=True)
    else:
        shot_rec["active"] = cand_id

    save_candidates(film, data)
    return entry


def list_candidates(film: Path, *, shot: str | None = None) -> dict[str, Any]:
    """Return archived candidates (all shots or filtered to one shot)."""
    film = Path(film)
    data = load_candidates(film)
    shots = data.get("shots") or {}
    if shot:
        return {"film": film.name, "shots": {shot: shots.get(shot, {"active": None, "items": []})}}
    return {"film": film.name, "shots": shots}


def select_candidate(film: Path, shot: str, candidate_id: str) -> dict[str, Any]:
    """Restore an archived candidate video + task + contact sheet into active shot slots."""
    film = Path(film)
    data = load_candidates(film)
    shot_rec = (data.get("shots") or {}).get(shot)
    if not shot_rec:
        raise FileNotFoundError(f"No archived candidates for shot {shot}")

    match = next((it for it in (shot_rec.get("items") or []) if it.get("candidate_id") == candidate_id), None)
    if match is None:
        raise FileNotFoundError(f"Candidate '{candidate_id}' not found for {shot}")

    vid_rel = match.get("video")
    if not vid_rel or not (film / vid_rel).is_file():
        raise FileNotFoundError(f"Candidate video missing on disk: {vid_rel}")

    # Preserve current active video first if it is unarchived so switching never drops work
    cur_vid = film / "shots" / f"{shot}.mp4"
    if cur_vid.is_file():
        archive_current_shot(film, shot, note="auto-archived before candidate switch", clear_active=False)
        data = load_candidates(film)
        shot_rec = data["shots"][shot]

    active_vid = film / "shots" / f"{shot}.mp4"
    active_vid.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(film / vid_rel, active_vid)

    sheet_rel = match.get("contact_sheet")
    if sheet_rel and (film / sheet_rel).is_file():
        active_sheet = film / "qa" / "h3-contact-sheets" / f"{shot}.jpg"
        active_sheet.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(film / sheet_rel, active_sheet)

    task_rel = match.get("task")
    if task_rel and (film / task_rel).is_file():
        active_task = film / "tasks" / f"{shot}.task.json"
        active_task.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(film / task_rel, active_task)

    shot_rec["active"] = candidate_id
    save_candidates(film, data)
    return {
        "ok": True,
        "shot": shot,
        "candidate_id": candidate_id,
        "video": str(active_vid.relative_to(film)),
        "video_sha256": file_sha256(active_vid),
        "visual_status": _current_visual_status(film, shot, file_sha256(active_vid)),
    }
