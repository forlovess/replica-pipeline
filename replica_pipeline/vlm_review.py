"""Independent VLM visual review for pre-H3 stills and post-H3 video contact sheets.

Separates generation from QA: an independent VLM call inspects candidate images
against the product size diagram, worn-scale reference, and S01 canon still,
writing sha256-bound verdicts to qa/VLM-STILL-REVIEW.json or qa/H3-VISUAL-LOCK.json.

Supported backends:
- 'codex' (default when REPLICA_VLM_URL is unset): local `codex exec -i ... --output-schema`
- 'openai': any OpenAI-compatible chat/completions vision endpoint via
  REPLICA_VLM_URL, REPLICA_VLM_KEY, REPLICA_VLM_MODEL
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .video_qa import (
        CONTACT_DIR_NAME,
        VISUAL_LOCK_NAME,
        build_visual_lock,
        extract_shot_contact_sheet,
        file_sha256,
    )
except ImportError:
    from video_qa import (
        CONTACT_DIR_NAME,
        VISUAL_LOCK_NAME,
        build_visual_lock,
        extract_shot_contact_sheet,
        file_sha256,
    )

VLM_STILL_LOCK_NAME = "qa/VLM-STILL-REVIEW.json"

STILL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "size_match", "identity_match", "person_match", "no_extra_tags", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "size_match": {"type": "boolean"},
        "identity_match": {"type": "boolean"},
        "person_match": {"type": "boolean"},
        "no_extra_tags": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}

VIDEO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict",
        "size_stable",
        "size_vs_spec",
        "identity_intact",
        "no_baked_captions",
        "no_hallucinated_tags",
        "person_consistent",
        "issues",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "size_stable": {"type": "boolean"},
        "size_vs_spec": {"type": "string", "enum": ["match", "too_big", "too_small", "drift"]},
        "identity_intact": {"type": "boolean"},
        "no_baked_captions": {"type": "boolean"},
        "no_hallucinated_tags": {"type": "boolean"},
        "person_consistent": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}


def _resolve_provider(provider: str) -> str:
    p = (provider or "auto").strip().lower()
    if p != "auto":
        return p
    if os.environ.get("REPLICA_VLM_URL") and os.environ.get("REPLICA_VLM_KEY"):
        return "openai"
    return "codex"


def _call_codex_vlm(images: list[Path], prompt: str, schema: dict[str, Any], *, timeout: int = 120) -> tuple[dict[str, Any], str]:
    cli = shutil.which("codex")
    if not cli:
        raise RuntimeError("codex CLI not found on PATH for VLM review")
    with tempfile.TemporaryDirectory(prefix="replica-vlm-") as td:
        sp = Path(td) / "schema.json"
        op = Path(td) / "out.json"
        sp.write_text(json.dumps(schema))
        cmd = [
            cli,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
        ]
        for img in images:
            cmd.extend(["-i", str(img)])
        cmd.extend(["--output-schema", str(sp), "-o", str(op), "-"])
        res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 or not op.is_file():
            detail = (res.stderr or res.stdout or "").strip()[-300:]
            raise RuntimeError(f"codex VLM review failed (rc={res.returncode}): {detail}")
        return json.loads(op.read_text()), "codex-cli"


def _call_openai_vlm(images: list[Path], prompt: str, schema: dict[str, Any], *, timeout: int = 120) -> tuple[dict[str, Any], str]:
    url = os.environ.get("REPLICA_VLM_URL", "").strip()
    key = os.environ.get("REPLICA_VLM_KEY", "").strip()
    model = os.environ.get("REPLICA_VLM_MODEL", "gpt-4o").strip()
    if not url or not key:
        raise RuntimeError("REPLICA_VLM_URL and REPLICA_VLM_KEY are required for openai VLM provider")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        ext = img.suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(img.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "vlm_review", "strict": True, "schema": schema},
        },
        "temperature": 0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        parsed = json.loads(resp.read().decode())
    raw_txt = parsed["choices"][0]["message"]["content"]
    return json.loads(raw_txt), f"openai:{model}"


def invoke_vlm(images: list[Path], prompt: str, schema: dict[str, Any], *, provider: str = "auto") -> tuple[dict[str, Any], str]:
    chosen = _resolve_provider(provider)
    if chosen == "codex":
        return _call_codex_vlm(images, prompt, schema)
    if chosen == "openai":
        return _call_openai_vlm(images, prompt, schema)
    raise ValueError(f"Unsupported VLM provider: {chosen!r}")


def _gather_still_refs(film: Path, shot: str) -> tuple[list[tuple[str, Path]], dict[str, Any]]:
    refs: list[tuple[str, Path]] = []
    size_ctx: dict[str, Any] = {}
    size_p = film / "qa" / "SIZE-LOCK.json"
    if size_p.is_file():
        size_ctx = json.loads(size_p.read_text())
        for key in ("size_diagram", "worn_scale_ref"):
            rel = size_ctx.get(key)
            if rel:
                for cand in (Path(rel), film / rel, film.parent / rel):
                    if cand.is_file():
                        refs.append((key, cand))
                        break
        canon_rel = size_ctx.get("canon_still")
        if canon_rel:
            for cand in (Path(canon_rel), film / canon_rel, film.parent / canon_rel):
                if cand.is_file() and f"{shot}-first" not in cand.name:
                    refs.append(("canon_still", cand))
                    break
    return refs, size_ctx


def _load_person_traits(film: Path) -> str:
    pp = film / "qa" / "PERSON-LOCK.json"
    if not pp.is_file():
        return ""
    try:
        pdata = json.loads(pp.read_text())
        return json.dumps(pdata.get("traits") or {}, ensure_ascii=False)
    except Exception:
        return ""


def review_still_with_vlm(
    film: Path,
    shot: str,
    *,
    provider: str = "auto",
) -> dict[str, Any]:
    """Run independent VLM QA on first-frames/h3/{shot}-first.jpg and persist to qa/VLM-STILL-REVIEW.json."""
    film = Path(film)
    still = film / "first-frames" / "h3" / f"{shot}-first.jpg"
    if not still.is_file():
        raise FileNotFoundError(f"missing still: {still}")

    refs, size_ctx = _gather_still_refs(film, shot)
    images = [p for _, p in refs] + [still]
    target_idx = len(images)

    ref_desc = "\n".join(
        f"- Image {i + 1}: {label} ({p.name})" for i, (label, p) in enumerate(refs)
    )
    cm = size_ctx.get("physical_cm") or {}
    cm_str = f"{cm.get('w')}x{cm.get('h')}x{cm.get('d')}cm" if cm else size_ctx.get("phrase", "target product")
    worn_rule = size_ctx.get("worn_rule") or (
        "judge product proportions strictly against physical_cm, size_diagram, worn_scale_ref, and canon_still "
        "(must stay consistent across body/handheld scale; not oversized, not shrunken toy/charm)."
    )
    traits = _load_person_traits(film)
    phrase = size_ctx.get("phrase") or ""

    prompt = (
        "You are an independent strict visual QA reviewer (NOT the image generator).\n"
        f"Reference images:\n{ref_desc or '- (none)'}\n"
        f"- Image {target_idx}: CANDIDATE STILL for shot {shot} ({still.name}).\n\n"
        f"Target product spec: {cm_str}.\n"
        f"SIZE AUTHORITY (always override generic/guessed ratios): {worn_rule}\n"
        + (f"Size phrase: {phrase}\n" if phrase else "")
        + "When worn_scale_ref / size_diagram / canon_still are provided, judge scale against those images "
        "and SIZE AUTHORITY strictly.\n"
        + (f"Locked presenter traits: {traits}\n" if traits else "")
        + f"\nEvaluate ONLY Image {target_idx} strictly:\n"
        "1. size_match: Product matches SIZE AUTHORITY + ref images for this framing "
        "(worn matches worn_scale_ref/canon_still; handheld matches physical_cm / forearm). "
        "Mark false for oversized travel-size bloat, undersized charm/toy shrinkage, "
        "or clear mismatch vs worn_scale_ref/canon_still. Close-up framing that fills the frame is OK "
        "if hand/body scale still matches physical_cm.\n"
        "2. identity_match: All product design details match reference (pattern, applique, ears/pearls, bow, lace, no wrong color bow).\n"
        "3. person_match: Presenter matches canon_still / locked traits (if a person is visible; true for product-only close-ups). "
        "Sleeve length on a hand-only crop is not a fail if skin/jewelry/outfit color match canon.\n"
        "4. no_extra_tags: No hallucinated SKU stickers, watermarks, or baked text overlays.\n"
        "Return strict JSON. verdict='PASS' ONLY if all four booleans are true."
    )

    result, used_provider = invoke_vlm(images, prompt, STILL_SCHEMA, provider=provider)
    all_ok = all(
        result.get(k) is True
        for k in ("size_match", "identity_match", "person_match", "no_extra_tags")
    )
    verdict = "PASS" if (result.get("verdict") == "PASS" and all_ok) else "FAIL"

    lock_path = film / VLM_STILL_LOCK_NAME
    if lock_path.is_file():
        doc = json.loads(lock_path.read_text())
    else:
        doc = {"version": 1, "shots": {}}

    doc.setdefault("shots", {})[shot] = {
        "verdict": verdict,
        "reviewer": f"vlm:{used_provider}",
        "still_sha256": file_sha256(still),
        "refs_sha256": {label: file_sha256(p) for label, p in refs},
        "size_match": bool(result.get("size_match")),
        "identity_match": bool(result.get("identity_match")),
        "person_match": bool(result.get("person_match")),
        "no_extra_tags": bool(result.get("no_extra_tags")),
        "issues": list(result.get("issues") or []),
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return doc["shots"][shot]


def review_video_with_vlm(
    film: Path,
    shot: str,
    *,
    provider: str = "auto",
) -> dict[str, Any]:
    """Run independent VLM QA on 4-frame contact sheet of shots/{shot}.mp4 and write qa/H3-VISUAL-LOCK.json."""
    film = Path(film)
    vid = film / "shots" / f"{shot}.mp4"
    if not vid.is_file():
        raise FileNotFoundError(f"missing shot video: {vid}")
    sheet = film / CONTACT_DIR_NAME / f"{shot}.jpg"
    if not sheet.is_file():
        sheet = extract_shot_contact_sheet(film, shot)

    refs, size_ctx = _gather_still_refs(film, shot)
    still = film / "first-frames" / "h3" / f"{shot}-first.jpg"
    if still.is_file():
        refs.append(("shot_first_frame", still))
    images = [p for _, p in refs] + [sheet]
    target_idx = len(images)

    ref_desc = "\n".join(
        f"- Image {i + 1}: {label} ({p.name})" for i, (label, p) in enumerate(refs)
    )
    cm = size_ctx.get("physical_cm") or {}
    cm_str = f"{cm.get('w')}x{cm.get('h')}x{cm.get('d')}cm" if cm else size_ctx.get("phrase", "target product")
    worn_rule = size_ctx.get("worn_rule") or (
        "judge product proportions strictly against physical_cm, size_diagram, worn_scale_ref, and canon_still "
        "(must stay consistent across body/handheld scale; not oversized, not shrunken)."
    )

    prompt = (
        "You are an independent strict video QA reviewer.\n"
        f"Reference images:\n{ref_desc or '- (none)'}\n"
        f"- Image {target_idx}: 4-FRAME HORIZONTAL CONTACT SHEET (10%, 35%, 65%, 90%) from generated video {shot}.\n\n"
        f"Target product spec: {cm_str}.\n"
        f"SIZE AUTHORITY: {worn_rule}\n"
        f"Inspect all 4 frames in Image {target_idx} strictly:\n"
        "1. size_stable: Product size stays consistent across all 4 frames (false if it shrinks/bloats mid-clip).\n"
        "2. size_vs_spec: 'match' if scale matches SIZE AUTHORITY + refs across frames, else 'too_big', 'too_small', or 'drift'.\n"
        "3. identity_intact: Product colors/applique/ears/bow/lace stay intact across all 4 frames.\n"
        "4. no_baked_captions: No AI-generated Chinese/English subtitle text burned into the video frames.\n"
        "5. no_hallucinated_tags: No invented SKU stickers or tags on the product.\n"
        "6. person_consistent: Presenter stays the same person/outfit across frames and matches canon.\n"
        "Return strict JSON. verdict='PASS' ONLY if all booleans are true and size_vs_spec=='match'."
    )

    result, used_provider = invoke_vlm(images, prompt, VIDEO_SCHEMA, provider=provider)
    all_ok = (
        result.get("size_vs_spec") == "match"
        and all(
            result.get(k) is True
            for k in (
                "size_stable",
                "identity_intact",
                "no_baked_captions",
                "no_hallucinated_tags",
                "person_consistent",
            )
        )
    )
    verdict = "PASS" if (result.get("verdict") == "PASS" and all_ok) else "FAIL"

    lock_path = film / VISUAL_LOCK_NAME
    doc = json.loads(lock_path.read_text()) if lock_path.is_file() else build_visual_lock()
    doc.setdefault("shots", {})[shot] = {
        "status": verdict,
        "reviewer": f"vlm:{used_provider}",
        "shot_sha256": file_sha256(vid),
        "contact_sheet": str(sheet.relative_to(film)),
        "size_stable": bool(result.get("size_stable")),
        "size_vs_spec": str(result.get("size_vs_spec") or "drift"),
        "identity_intact": bool(result.get("identity_intact")),
        "no_baked_captions": bool(result.get("no_baked_captions")),
        "no_hallucinated_tags": bool(result.get("no_hallucinated_tags")),
        "person_consistent": bool(result.get("person_consistent")),
        "issues": list(result.get("issues") or []),
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return doc["shots"][shot]


def validate_vlm_still_review_data(
    data: dict[str, Any],
    film: Path,
    shots: list,
    *,
    require_vlm_reviewer: bool = True,
) -> list[str]:
    errs: list[str] = []
    shot_map = data.get("shots") or {}
    for s in shots:
        sid = s["id"]
        entry = shot_map.get(sid)
        if not isinstance(entry, dict):
            errs.append(
                f"VLM-STILL-REVIEW.json: {sid} missing — run `vlm-review-still --shot {sid}`"
            )
            continue
        if require_vlm_reviewer and not str(entry.get("reviewer") or "").startswith("vlm:"):
            errs.append(f"VLM-STILL-REVIEW.json: {sid} reviewer must start with 'vlm:'")
        if str(entry.get("verdict") or "").upper() != "PASS":
            issues = "; ".join(entry.get("issues") or [])
            errs.append(f"VLM-STILL-REVIEW.json: {sid} verdict!=PASS" + (f" ({issues})" if issues else ""))
            continue
        for flag in ("size_match", "identity_match", "person_match", "no_extra_tags"):
            if entry.get(flag) is not True:
                errs.append(f"VLM-STILL-REVIEW.json: {sid} {flag} must be true")
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        rec_sha = entry.get("still_sha256")
        if not rec_sha:
            errs.append(f"VLM-STILL-REVIEW.json: {sid} missing still_sha256")
        elif still.is_file() and file_sha256(still) != rec_sha:
            errs.append(
                f"VLM-STILL-REVIEW.json: {sid} still_sha256 mismatch — still changed since VLM review"
            )
        refs_sha = entry.get("refs_sha256") or {}
        if refs_sha:
            cur_refs = {label: file_sha256(p) for label, p in _gather_still_refs(film, sid)[0]}
            for label, exp_sha in refs_sha.items():
                if label not in cur_refs:
                    errs.append(f"VLM-STILL-REVIEW.json: {sid} reference '{label}' missing since VLM review")
                elif cur_refs[label] != exp_sha:
                    errs.append(
                        f"VLM-STILL-REVIEW.json: {sid} reference '{label}' sha256 mismatch — "
                        "canon/scale ref changed since VLM review"
                    )
    return errs
