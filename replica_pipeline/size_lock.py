"""Film-level product size lock precedence.

Authority order (never invent a third scale):
1. **user_spec** — user/Brief/PRODUCT-STILL-RULE gave explicit size
   (cm, worn fraction, or a named scale board). Whole film follows that.
2. **first_frame** — no explicit size said → S01 accepted still is the
   visual canon; every later still matches that frame's product scale.

Do not mix: if user_spec exists, do not let S01 "look bigger" win.
If only first_frame, do not invent cm numbers that fight the picture.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SOURCE_USER = "user_spec"
SOURCE_FRAME = "first_frame"
VALID_SOURCES = {SOURCE_USER, SOURCE_FRAME}


def default_phrase_from_physical(cm: dict | None) -> str:
    if not cm:
        return ""
    w, h, d = cm.get("w"), cm.get("h"), cm.get("d")
    if w and h and d:
        return f"迷你约{w}×{h}×{d}cm；上身包宽约胸宽1/3–2/5"
    return ""


def build_size_lock(
    *,
    source: str,
    canon_still: str,
    shots: dict[str, str],
    physical_cm: dict | None = None,
    phrase: str | None = None,
    worn_rule: str | None = None,
    worn_scale_ref: str | None = None,
    user_spec_text: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be {SOURCE_USER!r} or {SOURCE_FRAME!r}")
    data: dict[str, Any] = {
        "source": source,
        "canon_still": canon_still,
        "shots": shots,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if worn_scale_ref:
        data["worn_scale_ref"] = worn_scale_ref
    if physical_cm:
        data["physical_cm"] = physical_cm
    phrase = phrase or default_phrase_from_physical(physical_cm)
    if phrase:
        data["phrase"] = phrase
    if worn_rule:
        data["worn_rule"] = worn_rule
    if user_spec_text:
        data["user_spec_text"] = user_spec_text
    if note:
        data["note"] = note
    # Authority hint for agents
    if source == SOURCE_USER:
        data["authority"] = (
            "User-specified size is global. Match physical_cm / phrase / worn_rule "
            "on every still; canon_still is a visual board, not a competing scale."
        )
    else:
        data["authority"] = (
            "No user size given — first accepted still (canon_still) is the scale. "
            "Later stills must match that product size against body/hands/frame."
        )
    return data


def write_size_lock(film: Path, data: dict[str, Any]) -> Path:
    film = Path(film)
    path = film / "qa" / "SIZE-LOCK.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def validate_size_lock_data(data: dict, film: Path, shots: list) -> list[str]:
    errs: list[str] = []
    source = data.get("source")
    if source not in VALID_SOURCES:
        errs.append(
            "SIZE-LOCK.json: source must be 'user_spec' (user gave size) "
            "or 'first_frame' (no size said → S01 still wins)"
        )
        source = None

    canon = data.get("canon_still") or data.get("canon")
    if not canon:
        errs.append("SIZE-LOCK.json: missing canon_still")
    else:
        canon_path = film / canon if not Path(canon).is_absolute() else Path(canon)
        if not canon_path.is_file():
            alt = film.parent / canon
            if not alt.is_file():
                errs.append(f"SIZE-LOCK.json: canon_still missing on disk: {canon}")

    if source == SOURCE_USER:
        has_spec = bool(
            data.get("physical_cm")
            or data.get("phrase")
            or data.get("worn_rule")
            or data.get("user_spec_text")
        )
        if not has_spec:
            errs.append(
                "SIZE-LOCK.json: source=user_spec but missing physical_cm/phrase/"
                "worn_rule/user_spec_text"
            )
    elif source == SOURCE_FRAME:
        # Visual canon is enough; optional phrase describing what is seen in S01
        if not canon:
            errs.append("SIZE-LOCK.json: source=first_frame requires canon_still")

    if source == SOURCE_USER and not (
        data.get("phrase") or data.get("worn_rule") or data.get("user_spec_text")
    ):
        # physical_cm alone is ok if we can form phrase — already checked has_spec
        pass
    elif source == SOURCE_FRAME and not data.get("phrase") and not data.get("worn_rule"):
        # Allow empty phrase when matching the picture only
        pass
    elif source is None:
        if not data.get("phrase") and not data.get("worn_rule"):
            errs.append("SIZE-LOCK.json: missing phrase or worn_rule (scale copy)")

    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        st = shot_map.get(sid)
        if isinstance(st, dict):
            st = st.get("size") or st.get("status")
        if str(st).upper() != "PASS":
            errs.append(f"SIZE-LOCK.json: {sid} size not PASS")
    return errs
