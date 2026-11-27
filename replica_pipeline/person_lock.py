"""Film-level presenter consistency (same remake person across shots).

Do not freely reinvent the presenter each cut. Once the remake person is
set (usually the accepted S01 still), later shots must stay that person:
same gender / age band / hair / outfit / recognizable likeness.

- Initial cast still follows source notes in `presenter-place.md`
  (do not invent a new beauty type / place).
- Continuity authority for S02+ is the **remake canon still**, not
  pixel-matching the source face, and not a fresh improvisation.

Degree: same remake person continuity — not source face-ID, not free casting.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

REQUIRED_TRAITS = ("gender", "apparent_age", "hair", "outfit")


def build_person_lock(
    *,
    canon_still: str,
    traits: dict[str, str],
    shots: dict[str, str],
    place: str | None = None,
    presenter_place_md: str = "qa/presenter-place.md",
    note: str = "",
) -> dict[str, Any]:
    missing = [k for k in REQUIRED_TRAITS if not (traits.get(k) or "").strip()]
    if missing:
        raise ValueError(f"PERSON-LOCK traits missing: {missing}")
    data: dict[str, Any] = {
        "canon_still": canon_still,
        "presenter_place_md": presenter_place_md,
        "traits": {k: traits[k].strip() for k in REQUIRED_TRAITS},
        "shots": shots,
        "rule": (
            "Stay consistent with the remake person locked in canon_still (usually S01). "
            "No free casting: do not invent a new adult / hair / outfit per shot."
        ),
        "degree": (
            "Match the remake canon person (recognizable + wardrobe continuity). "
            "Not source face-ID; not free improvisation. "
            "Fail if a later shot looks like a different adult or new outfit."
        ),
        "authority": (
            "Remake canon_still wins for continuity. presenter-place.md only set the "
            "initial cast from source; do not wander away from the locked remake person."
        ),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if place:
        data["place"] = place.strip()
    if note:
        data["note"] = note
    return data


def write_person_lock(film: Path, data: dict[str, Any]) -> Path:
    film = Path(film)
    path = film / "qa" / "PERSON-LOCK.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def validate_person_lock_data(data: dict, film: Path, shots: list) -> list[str]:
    errs: list[str] = []
    canon = data.get("canon_still") or data.get("canon")
    if not canon:
        errs.append("PERSON-LOCK.json: missing canon_still (usually S01 still)")
    else:
        canon_path = film / canon if not Path(canon).is_absolute() else Path(canon)
        if not canon_path.is_file():
            alt = film.parent / canon
            if not alt.is_file():
                errs.append(f"PERSON-LOCK.json: canon_still missing on disk: {canon}")

    pp = data.get("presenter_place_md") or "qa/presenter-place.md"
    if not (film / pp).is_file():
        errs.append(f"PERSON-LOCK.json: missing {pp}")

    traits = data.get("traits") or {}
    for k in REQUIRED_TRAITS:
        if not str(traits.get(k) or "").strip():
            errs.append(f"PERSON-LOCK.json: traits.{k} required")

    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        st = shot_map.get(sid)
        if isinstance(st, dict):
            st = st.get("person") or st.get("status")
        if str(st).upper() != "PASS":
            errs.append(f"PERSON-LOCK.json: {sid} person not PASS")
    return errs
