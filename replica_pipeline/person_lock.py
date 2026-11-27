"""Film-level presenter consistency (same person across shots).

Source films keep one presenter through the piece. Remakes must too:
gender / apparent age / hair / outfit / overall likeness stay locked—
not a new adult each cut. Degree: recognizable same person, not
pixel-perfect face ID; recasting or outfit swaps without source basis = FAIL.

Authority:
1. `qa/presenter-place.md` from source read (step 1) — who/where to match.
2. Remake visual board = first accepted person still (usually S01)
   written as `canon_still` in PERSON-LOCK.json.
3. Later stills attach that canon still as a reference and keep the same
   wardrobe + hair + age band.
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
            "Same presenter across the film (source-level consistency). "
            "Match traits + canon_still likeness; do not recast adult / hair / outfit "
            "per shot unless the source itself changes wardrobe mid-film."
        ),
        "degree": (
            "Recognizable same person and wardrobe continuity — not pixel face-ID. "
            "Fail if a later shot looks like a different adult, different hair length/color "
            "band, or a new outfit without source basis."
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
