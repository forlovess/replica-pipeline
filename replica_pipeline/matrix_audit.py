"""Cross-film duplicate detection for batch exports."""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

try:
    from .script_lint import compile_shot_text, read_strict_json, speech_chars, vo_remake_text
    from .video_qa import file_sha256
except ImportError:
    from script_lint import compile_shot_text, read_strict_json, speech_chars, vo_remake_text
    from video_qa import file_sha256


def _ngrams(text: str) -> set[str]:
    return {text[index:index + size] for size in (2, 3) for index in range(len(text) - size + 1)}


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    first, second = _ngrams(left), _ngrams(right)
    return len(first & second) / max(1, len(first | second))


def _film_evidence(film: Path) -> dict:
    qa = film / "qa"
    plan = qa / "REBEAT-PLAN.json"
    shots = read_strict_json(plan).get("shots") or [] if plan.is_file() else []
    first = shots[0] if shots else {}
    remake = qa / "VO-REMAKE.md"
    script = vo_remake_text(remake.read_text()) if remake.is_file() else "\n".join(row.get("spoken") or row.get("vo") or "" for row in shots)
    script = re.sub(r"</?[A-Z][A-Z0-9_]*>", "", script)
    spoken = speech_chars(compile_shot_text(script)["spoken"])
    hook_text = first.get("hook") or first.get("spoken") or first.get("vo") or ""
    hook = speech_chars(compile_shot_text(hook_text)["spoken"])[:13] if hook_text else spoken[:13]
    size = qa / "SIZE-LOCK.json"
    canon_ref = read_strict_json(size).get("canon_still") if size.is_file() else None
    canon = next((path for path in (film / canon_ref, film.parent / canon_ref) if path.is_file()), None) if canon_ref else None
    if canon is None:
        canon = next((film / rel for rel in ("first-frames/h3/S01-first.jpg", "first-frames/S01.jpg") if (film / rel).is_file()), None)
    check = qa / "assemble-check.json"
    bgm_ref = read_strict_json(check).get("bgm_path") if check.is_file() else None
    bgm = film / bgm_ref if bgm_ref else next((path for folder in (qa, film) for path in sorted(folder.glob("bgm.*"))
                                                              if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}), None)
    return {"bgm": file_sha256(bgm) if bgm and bgm.is_file() else None,
            "canon_still": file_sha256(canon) if canon else None, "vo": spoken, "hook": hook}


def audit_matrix(batch_dirs, *, out_dir=None, policy_path=None) -> dict:
    roots = [Path(batch_dirs)] if isinstance(batch_dirs, (str, Path)) else [Path(item) for item in batch_dirs]
    if not roots:
        raise ValueError("audit-matrix requires a batch directory")
    films = sorted({path.resolve() for root in roots for path in root.iterdir() if path.is_dir() and re.match(r"^\d{2}-", path.name)})
    if not films:
        raise ValueError("no numbered films found in batch directories")
    policy = read_strict_json(Path(policy_path)) if policy_path else {}
    threshold = policy.get("similarity_threshold", 0.70)
    if type(threshold) not in (int, float) or not 0 < threshold <= 1:
        raise ValueError("matrix similarity_threshold must be in (0, 1]")
    allowed = policy.get("allowed_bgm_sha256") or []
    if not isinstance(allowed, list) or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in allowed):
        raise ValueError("allowed_bgm_sha256 must be a list of SHA-256 digests")
    evidence = {film: _film_evidence(film) for film in films}
    findings = []
    for left, right in itertools.combinations(films, 2):
        pair = [str(left), str(right)]
        first, second = evidence[left], evidence[right]
        for kind in ("bgm", "canon_still"):
            sha = first[kind]
            if sha and sha == second[kind]:
                excepted = kind == "bgm" and (sha in allowed or policy.get("allow_shared_bgm") is True)
                findings.append({"type": kind, "films": pair, "sha256": sha, "allowed": excepted})
        for kind in ("vo", "hook"):
            score = _similarity(first[kind], second[kind])
            if score >= threshold:
                findings.append({"type": kind, "films": pair, "similarity": round(score, 4), "allowed": False})
    result = {"ok": all(row["allowed"] for row in findings), "threshold": threshold, "films": list(map(str, films)), "findings": findings}
    dest = Path(out_dir) if out_dir else roots[0]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "MATRIX-AUDIT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    report = ["# Matrix Audit", "", f"{'PASS' if result['ok'] else 'FAIL'} — {len(films)} films", ""]
    for row in findings:
        report.append(f"- {'Allowed' if row['allowed'] else 'High risk'}: {row['type']} — {' vs '.join(row['films'])} ({row.get('similarity', row.get('sha256'))})")
    (dest / "MATRIX-AUDIT.md").write_text("\n".join(report) + "\n")
    return result
