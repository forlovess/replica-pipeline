"""Film-level product size lock precedence.

Authority order (never invent a third scale):
1. **user_spec** — user/Brief/PRODUCT-STILL-RULE gave explicit size
   (cm, worn fraction, or a named scale board). Whole film follows that.
2. **first_frame** — no explicit size said → S01 accepted still is the
   visual canon; every later still matches that frame's product scale.

Do not mix: if user_spec exists, do not let S01 "look bigger" win.
If only first_frame, do not invent cm numbers that fight the picture.

Hard conflict (user_spec + source-t0 swap):
  Source first frame locks person / place / pose / framing ONLY.
  Do **not** inherit the source product's bbox / travel-bag occupancy.
  Scale comes only from physical_cm + worn_rule + size diagram / worn-scale-ref.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

SOURCE_USER = "user_spec"
SOURCE_FRAME = "first_frame"
VALID_SOURCES = {SOURCE_USER, SOURCE_FRAME}

# Structured evidence required when source=user_spec (stops paper PASS).
VALID_MODES = {"worn", "handheld", "open", "detail", "other"}
VALID_VS_SOURCE = {"smaller", "n/a", "na", "not_applicable"}
VALID_VS_SPEC = {"match"}
SIZE_DIAGRAM_NEEDLES = (
    "front-with-size",
    "size-diagram",
    "size_diagram",
    "with-size",
    "尺标",
)
WORN_REF_NEEDLES = ("worn-scale-ref", "worn_scale", "上身比例")


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
    shots: dict[str, Any],
    physical_cm: dict | None = None,
    phrase: str | None = None,
    worn_rule: str | None = None,
    worn_scale_ref: str | None = None,
    size_diagram: str | None = None,
    user_spec_text: str | None = None,
    forbid_source_product_scale: bool | None = None,
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
    if size_diagram:
        data["size_diagram"] = size_diagram
    if physical_cm:
        data["physical_cm"] = physical_cm
    phrase = phrase or default_phrase_from_physical(physical_cm)
    if phrase:
        data["phrase"] = phrase
    if worn_rule:
        data["worn_rule"] = worn_rule
    if user_spec_text:
        data["user_spec_text"] = user_spec_text
    if forbid_source_product_scale is None:
        forbid_source_product_scale = source == SOURCE_USER
    data["forbid_source_product_scale"] = bool(forbid_source_product_scale)
    if note:
        data["note"] = note
    # Authority hint for agents
    if source == SOURCE_USER:
        data["authority"] = (
            "User-specified size is global. Match physical_cm / phrase / worn_rule "
            "on every still; canon_still is a visual board, not a competing scale. "
            "Source-t0 swap must NOT inherit source product bbox / travel-bag occupancy."
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
    data = dict(data)
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def _shot_status(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("size") or entry.get("status")
    if entry is None:
        return None
    return str(entry)


def _refs_blob(data: dict, entry: dict) -> str:
    parts: list[str] = []
    for key in ("size_diagram", "worn_scale_ref", "user_spec_text", "phrase"):
        v = data.get(key)
        if v:
            parts.append(str(v))
    refs = entry.get("refs_used") or entry.get("refs") or []
    if isinstance(refs, str):
        refs = [refs]
    parts.extend(str(r) for r in refs)
    note = entry.get("note") or ""
    if note:
        parts.append(str(note))
    return " ".join(parts).lower()


def _has_needle(blob: str, needles: tuple[str, ...]) -> bool:
    return any(n.lower() in blob for n in needles)


def _normalize_vs_source(raw: Any) -> str:
    s = str(raw or "").strip().lower().replace(" ", "_")
    if s in {"na", "n/a", "not_applicable", "not-applicable", "none"}:
        return "n/a"
    return s


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_shot_size_evidence(
    sid: str,
    entry: Any,
    *,
    source: str | None,
    data: dict,
    film: Path | None = None,
) -> list[str]:
    """Per-shot checks. user_spec requires structured evidence, not a bare PASS string."""
    errs: list[str] = []
    status = _shot_status(entry)
    if str(status or "").upper() != "PASS":
        errs.append(f"SIZE-LOCK.json: {sid} size not PASS")
        return errs

    if source != SOURCE_USER:
        # first_frame: string PASS still OK (canon still is the scale)
        return errs

    if not isinstance(entry, dict):
        errs.append(
            f"SIZE-LOCK.json: {sid} must be an object under source=user_spec "
            "(got string PASS). Run: pipeline size-review … "
            "Fields: status, mode, vs_user_spec, vs_source_occupancy, refs_used"
        )
        return errs

    mode = str(entry.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        errs.append(
            f"SIZE-LOCK.json: {sid} mode must be one of {sorted(VALID_MODES)} "
            f"(got {mode!r})"
        )

    vs_spec = str(entry.get("vs_user_spec") or entry.get("vs_spec") or "").strip().lower()
    if vs_spec not in VALID_VS_SPEC:
        errs.append(
            f"SIZE-LOCK.json: {sid} vs_user_spec must be 'match' "
            f"(got {vs_spec!r}) — confirm still follows physical_cm / worn_rule"
        )

    vs_src = _normalize_vs_source(
        entry.get("vs_source_occupancy") or entry.get("vs_source")
    )
    if not vs_src:
        errs.append(
            f"SIZE-LOCK.json: {sid} missing vs_source_occupancy "
            "('smaller' when source product was larger; 'n/a' if no source product scale)"
        )
    elif vs_src in {"same", "larger", "equal", "bigger"}:
        errs.append(
            f"SIZE-LOCK.json: {sid} vs_source_occupancy={vs_src!r} forbidden under "
            "user_spec — must not inherit source product bbox / travel-bag occupancy "
            "(use 'smaller' or 'n/a')"
        )
    elif vs_src not in VALID_VS_SOURCE:
        errs.append(
            f"SIZE-LOCK.json: {sid} vs_source_occupancy must be "
            f"'smaller' or 'n/a' (got {vs_src!r})"
        )

    blob = _refs_blob(data, entry)
    if not _has_needle(blob, SIZE_DIAGRAM_NEEDLES):
        errs.append(
            f"SIZE-LOCK.json: {sid} refs_used must include size diagram "
            "(e.g. front-with-size.jpg) when source=user_spec"
        )
    if mode == "worn" and not (
        _has_needle(blob, WORN_REF_NEEDLES) or data.get("worn_scale_ref")
    ):
        errs.append(
            f"SIZE-LOCK.json: {sid} mode=worn requires worn-scale-ref "
            "(film worn_scale_ref or refs_used)"
        )

    recorded_hash = entry.get("still_sha256")
    if recorded_hash and film is not None:
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if still.is_file() and _file_sha256(still) != recorded_hash:
            errs.append(
                f"SIZE-LOCK.json: {sid} still_sha256 mismatch — still changed since size-review"
            )

    return errs


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
        # Hard flag: source product scale must not win
        if data.get("forbid_source_product_scale") is False:
            errs.append(
                "SIZE-LOCK.json: forbid_source_product_scale=false illegal under "
                "user_spec (source-t0 must not keep source product occupancy)"
            )
        elif "forbid_source_product_scale" not in data:
            errs.append(
                "SIZE-LOCK.json: set forbid_source_product_scale=true under user_spec "
                "(source first frame locks person/place/pose only — not product scale)"
            )
        # Prefer an explicit size diagram path when claimed
        diagram = data.get("size_diagram")
        if diagram:
            dpath = film / diagram if not Path(diagram).is_absolute() else Path(diagram)
            if not dpath.is_file():
                alt = film.parent / diagram
                if not alt.is_file():
                    # soft path miss: still allow if refs_used names it; warn as error
                    # only when nothing else will cite it — kept as error for honesty
                    errs.append(
                        f"SIZE-LOCK.json: size_diagram missing on disk: {diagram}"
                    )
    elif source == SOURCE_FRAME:
        if not canon:
            errs.append("SIZE-LOCK.json: source=first_frame requires canon_still")

    if source == SOURCE_USER and not (
        data.get("phrase") or data.get("worn_rule") or data.get("user_spec_text")
    ):
        pass
    elif source == SOURCE_FRAME and not data.get("phrase") and not data.get("worn_rule"):
        pass
    elif source is None:
        if not data.get("phrase") and not data.get("worn_rule"):
            errs.append("SIZE-LOCK.json: missing phrase or worn_rule (scale copy)")

    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        entry = shot_map.get(sid)
        errs.extend(
            validate_shot_size_evidence(sid, entry, source=source, data=data, film=film)
        )
    return errs


def apply_size_review(
    data: dict,
    shot: str,
    *,
    mode: str,
    vs_user_spec: str = "match",
    vs_source_occupancy: str = "smaller",
    refs_used: list[str] | None = None,
    note: str = "",
    status: str = "PASS",
    film: Path | None = None,
) -> dict:
    """Write structured evidence for one shot; returns updated SIZE-LOCK dict."""
    mode = mode.strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    vs_spec = vs_user_spec.strip().lower()
    if vs_spec not in VALID_VS_SPEC:
        raise ValueError("vs_user_spec must be 'match'")
    vs_src = _normalize_vs_source(vs_source_occupancy)
    if vs_src in {"same", "larger", "equal", "bigger"}:
        raise ValueError(
            "vs_source_occupancy cannot be same/larger under size-review "
            "(would inherit source product scale)"
        )
    if vs_src not in VALID_VS_SOURCE:
        raise ValueError("vs_source_occupancy must be 'smaller' or 'n/a'")

    shots = dict(data.get("shots") or {})
    entry: dict[str, Any] = {
        "status": status.upper(),
        "mode": mode,
        "vs_user_spec": vs_spec,
        "vs_source_occupancy": vs_src,
        "refs_used": list(refs_used or []),
    }
    if film is not None:
        still = Path(film) / "first-frames" / "h3" / f"{shot}-first.jpg"
        if still.is_file():
            entry["still_sha256"] = _file_sha256(still)
    if note:
        entry["note"] = note
    shots[shot] = entry
    data = dict(data)
    data["shots"] = shots
    if data.get("source") == SOURCE_USER:
        data["forbid_source_product_scale"] = True
        if not data.get("authority"):
            data["authority"] = (
                "User-specified size is global. Source-t0 swap must NOT inherit "
                "source product bbox / travel-bag occupancy."
            )
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return data
