"""Pre-H3 still inspection: geometry checks, comparison sheet, and structured product lock.

Closes the pre-H3 loophole where oversized or wrong-variant stills could pass
via bare 'PASS' text in STILL-PRODUCT-LOCK.md:
- Checks image dimensions, portrait orientation, and ~9:16 aspect ratio via ffprobe.
- Builds a LABELED qa/still-contact-sheet.jpg (size diagram + worn ref + canon +
  S01..Sxx side by side) with a sha256-bound qa/still-contact-sheet.json manifest,
  so a redrawn still invalidates the board instead of silently passing.
- Enforces structured qa/STILL-PRODUCT-LOCK.json bound by still_sha256.

Labels are rendered with a built-in 5x7 bitmap font because the pipeline must not
depend on ffmpeg `drawtext` (missing from many Homebrew builds) or Pillow.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from .video_qa import file_sha256
except ImportError:
    from video_qa import file_sha256

PRODUCT_LOCK_NAME = "qa/STILL-PRODUCT-LOCK.json"
STILL_SHEET_NAME = "qa/still-contact-sheet.jpg"
SHEET_MANIFEST_NAME = "qa/still-contact-sheet.json"

PAD_COLOR = "0x18181c"
LABEL_BG = (24, 24, 28)
LABEL_FG = (236, 236, 240)

# 5x7 bitmap font: only the glyphs needed for board labels.
_FONT: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "01010", "00100", "00100", "00100", "01010", "10001"),
    "Y": ("10001", "01010", "00100", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10011", "10101", "10101", "10101", "11001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


def render_label_ppm(text: str, width: int, height: int) -> bytes:
    """Render a centered label as a binary PPM (no font/Pillow/drawtext dependency)."""
    text = (text or " ").upper()
    glyphs = [_FONT.get(ch, _FONT[" "]) for ch in text]
    char_w, char_h = 5, 7
    max_scale = max(1, (height - 6) // char_h)
    fit_scale = max(1, (width - 8) // max(1, len(glyphs) * (char_w + 1)))
    scale = min(3, max_scale, fit_scale)

    text_w = max(1, len(glyphs) * (char_w + 1) * scale - scale)
    text_h = char_h * scale
    x0 = max(0, (width - text_w) // 2)
    y0 = max(0, (height - text_h) // 2)

    px = bytearray(bytes(LABEL_BG) * (width * height))

    def set_px(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            off = (y * width + x) * 3
            px[off : off + 3] = bytes(LABEL_FG)

    for gi, glyph in enumerate(glyphs):
        gx = x0 + gi * (char_w + 1) * scale
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        set_px(gx + col * scale + dx, y0 + row * scale + dy)

    header = f"P6\n{width} {height}\n255\n".encode()
    return header + bytes(px)


def probe_image(path: Path) -> dict[str, Any]:
    """Return width, height, and aspect_ratio for a still image via ffprobe."""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        text=True,
    ).strip()
    w_s, h_s = out.split("x")
    w, h = int(w_s), int(h_s)
    return {"width": w, "height": h, "aspect_ratio": round(w / h, 4) if h else 0.0}


def check_still_geometry(
    path: Path,
    *,
    min_width: int = 480,
    min_height: int = 800,
    ratio_range: tuple[float, float] = (0.50, 0.65),
) -> tuple[bool, dict[str, Any], list[str]]:
    """Verify still is a portrait ~9:16 image meeting minimum resolution."""
    errs: list[str] = []
    try:
        info = probe_image(path)
    except Exception as e:
        return False, {}, [f"ffprobe failed on {path.name}: {e}"]
    w, h, ar = info["width"], info["height"], info["aspect_ratio"]
    if w < min_width or h < min_height:
        errs.append(f"{path.name}: resolution {w}x{h} below minimum {min_width}x{min_height}")
    if h <= w:
        errs.append(f"{path.name}: must be portrait (got {w}x{h})")
    elif not (ratio_range[0] <= ar <= ratio_range[1]):
        errs.append(
            f"{path.name}: aspect ratio {ar:.3f} outside portrait 9:16 range {ratio_range}"
        )
    return len(errs) == 0, info, errs


def _first_existing(film: Path, rel: str) -> Path | None:
    for cand in (Path(rel), film / rel, film.parent / rel):
        if cand.is_file():
            return cand
    return None


def _resolve_ref_images(film: Path) -> list[tuple[str, Path]]:
    """Product-scale reference images for the board, in a stable review order."""
    refs: list[tuple[str, Path]] = []
    size_p = film / "qa" / "SIZE-LOCK.json"
    if not size_p.is_file():
        return refs
    try:
        sdata = json.loads(size_p.read_text())
    except Exception:
        return refs
    for key, label in (
        ("size_diagram", "SIZE-DIAGRAM"),
        ("worn_scale_ref", "WORN-SCALE-REF"),
        ("canon_still", "CANON"),
    ):
        rel = sdata.get(key)
        if not rel:
            continue
        p = _first_existing(film, str(rel))
        if p is not None and all(p != existing for _, existing in refs):
            refs.append((label, p))
    return refs


def build_still_comparison_sheet(
    film: Path,
    shots: list[str] | None = None,
    *,
    cols: int = 6,
    cell_w: int = 270,
    cell_h: int = 480,
    label_h: int = 28,
) -> Path:
    """Build labeled qa/still-contact-sheet.jpg plus its sha256 manifest.

    Layout is row-major: reference images first (size diagram, worn ref, canon),
    then S01..Sxx. The manifest records each cell's label, position, and source
    sha256 so the board can be proven fresh against the current stills.
    """
    film = Path(film)
    if shots is None:
        plan = film / "qa" / "REBEAT-PLAN.json"
        if plan.is_file():
            shots = [s["id"] for s in (json.loads(plan.read_text()).get("shots") or [])]
        else:
            shots = sorted(
                p.stem.replace("-first", "")
                for p in (film / "first-frames" / "h3").glob("S*-first.jpg")
            )

    items: list[tuple[str, Path]] = list(_resolve_ref_images(film))
    for sid in shots:
        p = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if p.is_file():
            items.append((sid, p))
    if not items:
        raise FileNotFoundError(
            "No stills or size references to build a comparison board "
            "(generate first-frames/h3/Sxx-first.jpg first)"
        )

    out_path = film / STILL_SHEET_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_path.parent / ".still-sheet-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    img_h = max(1, cell_h - label_h)

    cells: list[Path] = []
    manifest_cells: list[dict[str, Any]] = []
    rows: list[Path] = []
    try:
        for idx, (label, src) in enumerate(items):
            lbl = tmp_dir / f"lbl_{idx:02d}.ppm"
            lbl.write_bytes(render_label_ppm(label, cell_w, label_h))
            cell = tmp_dir / f"cell_{idx:02d}.jpg"
            vf = (
                f"[0:v]scale={cell_w}:{img_h}:force_original_aspect_ratio=decrease,"
                f"pad={cell_w}:{img_h}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR}[img];"
                f"[img][1:v]vstack=inputs=2[out]"
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(src),
                    "-i",
                    str(lbl),
                    "-filter_complex",
                    vf,
                    "-map",
                    "[out]",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(cell),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            cells.append(cell)
            row_i, col_i = divmod(idx, cols)
            manifest_cells.append(
                {
                    "index": idx,
                    "row": row_i,
                    "col": col_i,
                    "label": label,
                    "source": str(src.relative_to(film)) if film in src.parents else str(src),
                    "source_sha256": file_sha256(src),
                }
            )

        # Pad the final row with dark blanks so hstack widths match
        rem = len(cells) % cols
        if rem != 0:
            for k in range(cols - rem):
                blank = tmp_dir / f"blank_{k}.jpg"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={PAD_COLOR}:s={cell_w}x{cell_h}:d=1",
                        "-frames:v",
                        "1",
                        str(blank),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                cells.append(blank)

        for r_idx in range(0, len(cells), cols):
            chunk = cells[r_idx : r_idx + cols]
            if len(chunk) == 1:
                rows.append(chunk[0])
            else:
                row_p = tmp_dir / f"row_{r_idx}.jpg"
                cmd = ["ffmpeg", "-y"] + sum([["-i", str(c)] for c in chunk], []) + [
                    "-filter_complex",
                    f"hstack=inputs={len(chunk)}[out]",
                    "-map",
                    "[out]",
                    "-q:v",
                    "3",
                    str(row_p),
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                rows.append(row_p)

        if len(rows) == 1:
            rows[0].replace(out_path)
        else:
            cmd = ["ffmpeg", "-y"] + sum([["-i", str(r)] for r in rows], []) + [
                "-filter_complex",
                f"vstack=inputs={len(rows)}[out]",
                "-map",
                "[out]",
                "-q:v",
                "3",
                str(out_path),
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    finally:
        for p in tmp_dir.glob("*"):
            p.unlink(missing_ok=True)
        tmp_dir.rmdir()

    sheet_sha = file_sha256(out_path)
    stills = {
        label: sha
        for label, sha in (
            (c["label"], c["source_sha256"]) for c in manifest_cells if c["label"].startswith("S")
        )
    }
    manifest = {
        "version": 1,
        "sheet": STILL_SHEET_NAME,
        "sheet_sha256": sheet_sha,
        "cols": cols,
        "rows": len(rows),
        "cell_w": cell_w,
        "cell_h": cell_h,
        "order": "row-major; references first, then S01..Sxx",
        "cells": manifest_cells,
        "stills": stills,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (film / SHEET_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return out_path


def validate_still_sheet(film: Path, shots: list) -> list[str]:
    """Require a fresh comparison board whose manifest still matches the current stills."""
    film = Path(film)
    sheet = film / STILL_SHEET_NAME
    man_p = film / SHEET_MANIFEST_NAME
    if not sheet.is_file():
        return [f"{STILL_SHEET_NAME} required (run: inspect-stills)"]
    if not man_p.is_file():
        return [f"{SHEET_MANIFEST_NAME} required (run: inspect-stills to bind the board)"]
    try:
        man = json.loads(man_p.read_text())
    except Exception as e:
        return [f"{SHEET_MANIFEST_NAME}: {e}"]

    errs: list[str] = []
    rec_sheet_sha = man.get("sheet_sha256")
    if not rec_sheet_sha:
        errs.append(f"{SHEET_MANIFEST_NAME}: missing sheet_sha256")
    elif file_sha256(sheet) != rec_sheet_sha:
        errs.append(f"{STILL_SHEET_NAME} modified since inspect-stills")

    stills = man.get("stills") or {}
    for s in shots:
        sid = s["id"]
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if not still.is_file():
            continue
        rec = stills.get(sid)
        if not rec:
            errs.append(f"{STILL_SHEET_NAME}: {sid} not on the board — rerun inspect-stills")
        elif file_sha256(still) != rec:
            errs.append(f"{STILL_SHEET_NAME}: {sid} stale — still changed since inspect-stills")
    return errs


def inspect_film_stills(film: Path) -> dict[str, Any]:
    """Run geometry checks on all stills and generate the labeled comparison board."""
    film = Path(film)
    plan = film / "qa" / "REBEAT-PLAN.json"
    if plan.is_file():
        shot_ids = [s["id"] for s in (json.loads(plan.read_text()).get("shots") or [])]
    else:
        shot_ids = sorted(
            p.stem.replace("-first", "")
            for p in (film / "first-frames" / "h3").glob("S*-first.jpg")
        )

    results: list[dict[str, Any]] = []
    present: list[str] = []
    all_ok = True
    for sid in shot_ids:
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if not still.is_file():
            results.append({"shot": sid, "missing": True, "ok": False})
            all_ok = False
            continue
        present.append(sid)
        ok, info, errs = check_still_geometry(still)
        if not ok:
            all_ok = False
        results.append(
            {
                "shot": sid,
                "still": str(still.relative_to(film)),
                "still_sha256": file_sha256(still),
                "ok": ok,
                "errors": errs,
                **info,
            }
        )

    out: dict[str, Any] = {"film": film.name, "shots": results}
    if not present:
        # No stills yet: report cleanly instead of crashing the CLI.
        out.update(
            {
                "ok": False,
                "comparison_sheet": None,
                "error": "no first-frames/h3/Sxx-first.jpg stills found to inspect",
            }
        )
        return out

    try:
        sheet = build_still_comparison_sheet(film, present)
    except Exception as e:  # ffmpeg missing / bad image — fail closed, not traceback
        out.update({"ok": False, "comparison_sheet": None, "error": f"board build failed: {e}"})
        return out

    out.update({"ok": all_ok, "comparison_sheet": str(sheet.relative_to(film))})
    return out


def build_product_lock(shots: dict[str, Any] | None = None, *, note: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "rule": (
            "Structured per-still product identity & geometry lock. Bare string PASS is forbidden; "
            "each shot is bound to still_sha256."
        ),
        "shots": dict(shots or {}),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if note:
        data["note"] = note
    return data


def apply_product_review(
    film: Path,
    data: dict[str, Any],
    shot: str,
    *,
    features_intact: bool = True,
    no_wrong_variant: bool = True,
    no_hallucinated_tags: bool = True,
    status: str = "PASS",
    note: str = "",
) -> dict[str, Any]:
    film = Path(film)
    still = film / "first-frames" / "h3" / f"{shot}-first.jpg"
    if not still.is_file():
        raise FileNotFoundError(f"missing still: {still}")
    geo_ok, geo_info, geo_errs = check_still_geometry(still)
    final_status = status.upper()
    if final_status == "PASS":
        if not geo_ok:
            raise ValueError("; ".join(geo_errs))
        if not (features_intact and no_wrong_variant and no_hallucinated_tags):
            raise ValueError("Cannot mark PASS when product check flag is false")

    shots = dict(data.get("shots") or {})
    entry: dict[str, Any] = {
        "status": final_status,
        "still_sha256": file_sha256(still),
        "geometry_ok": geo_ok,
        "width": geo_info.get("width"),
        "height": geo_info.get("height"),
        "features_intact": bool(features_intact),
        "no_wrong_variant": bool(no_wrong_variant),
        "no_hallucinated_tags": bool(no_hallucinated_tags),
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if note:
        entry["note"] = note
    shots[shot] = entry
    out = dict(data)
    out["shots"] = shots
    out["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out


def validate_product_lock_data(data: dict[str, Any], film: Path, shots: list) -> list[str]:
    errs: list[str] = []
    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        entry = shot_map.get(sid)
        if not isinstance(entry, dict):
            errs.append(
                f"STILL-PRODUCT-LOCK.json: {sid} must be a structured object "
                "(bare PASS forbidden; run product-review)"
            )
            continue
        if str(entry.get("status") or "").upper() != "PASS":
            errs.append(f"STILL-PRODUCT-LOCK.json: {sid} not PASS")
            continue
        for flag in ("geometry_ok", "features_intact", "no_wrong_variant", "no_hallucinated_tags"):
            if entry.get(flag) is not True:
                errs.append(f"STILL-PRODUCT-LOCK.json: {sid} {flag} must be true")
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        rec_sha = entry.get("still_sha256")
        if not rec_sha:
            errs.append(f"STILL-PRODUCT-LOCK.json: {sid} missing still_sha256")
        elif still.is_file() and file_sha256(still) != rec_sha:
            errs.append(
                f"STILL-PRODUCT-LOCK.json: {sid} still_sha256 mismatch — still changed since product-review"
            )
    return errs
