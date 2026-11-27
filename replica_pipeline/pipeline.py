#!/usr/bin/env python3
"""Fail-closed replica pipeline CLI.

Usage:
  python pipeline.py init [--batch DIR] [--film DIR]
  python pipeline.py status FILM_DIR
  python pipeline.py require FILM_DIR ACTION   # h3_submit|assemble|superres
  python pipeline.py set FILM_DIR STAGE STATUS [--note TEXT]
  python pipeline.py advance FILM_DIR         # mark current stage pass if artifacts ok
  python pipeline.py gate-check FILM_DIR STAGE
  python pipeline.py audit-batch [BATCH_DIR]
  python pipeline.py void-sparse7 FILM_DIR    # mark old 7-shot sparse work void
  python pipeline.py size-review FILM_DIR --shot S01 --mode worn \\
      --vs-source smaller --refs front-with-size.jpg,worn-scale-ref.jpg

Exit codes: 0 ok, 2 gate fail, 3 bad args.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import re
import sys
import time
from pathlib import Path

try:
    from .stages import (
        ACTION_REQUIRES,
        REQUIRED,
        STAGE_ORDER,
        STAGE_TITLE,
    )
except ImportError:
    from stages import (
        ACTION_REQUIRES,
        REQUIRED,
        STAGE_ORDER,
        STAGE_TITLE,
    )

PIPELINE_NAME = "qa/PIPELINE.json"
VERSION = 1

try:
    from .script_lint import compile_remake_text, compile_script_contract, read_strict_json, speech_chars
    from .video_qa import file_sha256
except ImportError:
    from script_lint import compile_remake_text, compile_script_contract, read_strict_json, speech_chars
    from video_qa import file_sha256


def _source_inputs(film: Path) -> dict:
    spec_path = film / "qa/source.json"
    spec = read_strict_json(spec_path)
    duration = spec.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("source.json: duration must be positive")
    paths = [spec_path, film / "qa/presenter-place.md"]
    for key in ("path", "cuts_path", "asr_path", "overview_path"):
        value = spec.get(key) or ("qa/cuts.json" if key == "cuts_path" else None)
        if not value:
            if key == "path":
                raise ValueError("source.json: missing source path")
            continue
        if not isinstance(value, str):
            raise ValueError(f"source.json: {key} must be a path string")
        path = film / value
        if not path.is_file():
            raise ValueError(f"source.json: referenced {key} is missing: {path}")
        paths.append(path)
    stem = spec.get("stem_split") or {}
    if not isinstance(stem, dict):
        raise ValueError("source.json: stem_split must be an object")
    for key in ("path", "bgm", "vocals"):
        value = stem.get(key)
        if value:
            if not isinstance(value, str) or not (film / value).is_file():
                raise ValueError(f"source.json: referenced stem_split.{key} is missing")
            paths.append(film / value)
    cuts_path = film / (spec.get("cuts_path") or "qa/cuts.json")
    cuts = read_strict_json(cuts_path).get("cuts")
    if not isinstance(cuts, list):
        raise ValueError("cuts.json: expected cuts array")
    previous = -1
    for cut in cuts:
        point = cut.get("t", cut.get("time")) if isinstance(cut, dict) else cut
        if type(point) not in (int, float) or not previous < point <= duration or point < 0:
            raise ValueError("cuts.json: cut times must be strictly ordered and in range")
        previous = point
    if spec.get("cuts_count", len(cuts)) != len(cuts):
        raise ValueError("source.json: cuts_count disagrees with cuts.json")
    asr = film / (spec.get("asr_path") or "qa/source-asr.json")
    if asr.is_file():
        paths.append(asr)
    recognized = _source_transcript(asr)
    if not recognized and spec.get("silent_source") is not True:
        raise ValueError("source-asr.json missing or empty; explicitly set silent_source=true for silent footage")
    return {str(path.relative_to(film)) if path.is_relative_to(film) else str(path): file_sha256(path)
            for path in sorted(set(paths))}


def _source_transcript(path: Path) -> str:
    if not path.is_file() or not path.stat().st_size:
        return ""
    try:
        from .asr_verify import transcript_text
    except ImportError:
        from asr_verify import transcript_text
    return speech_chars(transcript_text(read_strict_json(path)))


def validate_source(film: Path) -> list[str]:
    try:
        _source_inputs(Path(film))
    except (OSError, ValueError, TypeError) as exc:
        return [str(exc)]
    return []


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def film_pipeline(film: Path) -> Path:
    return film / PIPELINE_NAME


def load(film: Path) -> dict:
    p = film_pipeline(film)
    if not p.is_file():
        raise SystemExit(f"PIPELINE missing: {p}\nRun: pipeline.py init --film {film}")
    return json.loads(p.read_text())


def save(film: Path, data: dict) -> None:
    p = film_pipeline(film)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = now()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(p)


def blank_state(film_name: str) -> dict:
    stages = {
        s: {"status": "pending", "note": "", "updated": None}
        for s in STAGE_ORDER
    }
    return {
        "version": VERSION,
        "film": film_name,
        "skill": "replica",
        "current": STAGE_ORDER[0],
        "stages": stages,
        "locks": {
            "h3_submit": False,
            "assemble": False,
            "superres": False,
            "superres_user_allow": False,
        },
        "void": {
            "sparse7_template": False,
            "note": "",
        },
        "created": now(),
        "updated": now(),
    }


def discover_films(batch: Path) -> list[Path]:
    return sorted(
        p for p in batch.iterdir()
        if p.is_dir() and re.match(r"^\d{2}-", p.name)
    )


def artifacts_present(film: Path, stage: str) -> tuple[bool, list[str]]:
    missing = []
    for rel in REQUIRED.get(stage, []):
        if not (film / rel).is_file():
            missing.append(rel)
    if stage == "3_beat_plan":
        plan = film / "qa/REBEAT-PLAN.json"
        if plan.is_file():
            bad = validate_beat_plan(plan)
            missing.extend(bad)
    if stage == "4_stills":
        missing.extend(validate_stills(film))
    if stage == "5_h3":
        missing.extend(validate_h3(film))
    if stage == "6_assemble":
        missing.extend(validate_assemble(film))
    if stage == "2_vo_adapt":
        missing.extend(validate_vo_density(film))
    if stage == "7_superres":
        allow = film / "qa/superres-allow.json"
        if allow.is_file():
            try:
                evidence = read_strict_json(allow)
                if evidence.get("allow") is not True or evidence.get("by") != "user":
                    missing.append("qa/superres-allow.json:allow!=true")
            except Exception:
                missing.append("qa/superres-allow.json:invalid")
        if (film / "qa/SUPERRES.json").is_file():
            try:
                from .superres import validate_superres_data
            except ImportError:
                from superres import validate_superres_data
            missing.extend(validate_superres_data(film))
    return (len(missing) == 0, missing)


def validate_beat_plan(plan_path: Path) -> list[str]:
    errs = []
    try:
        plan = json.loads(plan_path.read_text())
    except Exception as e:
        return [f"REBEAT-PLAN.json: {e}"]
    shots = plan.get("shots") or []
    if len(shots) < 1:
        errs.append("REBEAT-PLAN: empty shots")
    # Ban fixed-7 template without cut/vo basis
    if len(shots) == 7 and plan.get("source_basis") in (None, "fixed7", "nov23_template"):
        errs.append("REBEAT-PLAN: 7 shots without source_basis (cuts/vo) — forbidden template")
    for s in shots:
        sec = s.get("sec")
        try:
            sec_f = float(sec)
        except Exception:
            errs.append(f"{s.get('id')}: bad sec")
            continue
        if not (4 <= sec_f <= 15):
            errs.append(f"{s.get('id')}: sec={sec_f} not in 4–15")
        if not s.get("source") and not plan.get("source_basis"):
            errs.append(f"{s.get('id')}: missing source basis")
    # Prefer explicit basis field
    if not plan.get("source_basis") and not any(s.get("source") for s in shots):
        errs.append("REBEAT-PLAN: set source_basis=scene_merge|vo_beat")
    return errs


def validate_vo_density(film: Path) -> list[str]:
    p = film / "qa/vo-density.json"
    if not p.is_file():
        return []
    errs = []
    try:
        d = read_strict_json(p)
    except Exception as e:
        return [f"vo-density.json: {e}"]
    src = d.get("source_chars") or 0
    rem = d.get("remake_chars") or 0
    source = film / "qa/source.json"
    script = film / "qa/VO-REMAKE.md"
    if source.is_file() and source.stat().st_size and script.is_file() and script.stat().st_size:
        try:
            spec = read_strict_json(source)
            actual_src = len(_source_transcript(film / (spec.get("asr_path") or "qa/source-asr.json")))
            actual_rem = len(speech_chars(compile_remake_text(script.read_text())))
            expected = actual_rem / actual_src if actual_src else 0
            ratio = d.get("ratio")
            if src != actual_src or rem != actual_rem or type(ratio) not in (int, float) or abs(ratio - expected) > 0.005:
                errs.append(f"vo-density.json: expected source_chars={actual_src}, remake_chars={actual_rem}, ratio={expected:.4f}")
        except (OSError, ValueError, TypeError) as exc:
            return [f"vo-density.json: {exc}"]
    if type(src) not in (int, float) or type(rem) not in (int, float):
        return ["vo-density.json: character counts must be numeric"]
    if src <= 0:
        return errs
    ratio = rem / src if src else 0
    d_ratio = d.get("ratio")
    if d_ratio is None:
        errs.append("vo-density.json: missing ratio")
    # Soft gate: if facts not exhausted, remake should be >= 0.8 of source
    if not d.get("facts_exhausted") and ratio < 0.8:
        errs.append(
            f"vo-density: remake/source={ratio:.2f} < 0.80 and facts_exhausted!=true "
            "(collapse into short lines forbidden)"
        )
    if d.get("looped_claims"):
        errs.append("vo-density: looped_claims=true forbidden")
    return errs


def validate_size_lock(film: Path, shots: list) -> list[str]:
    """Film-level product scale: user_spec wins; else first_frame (S01) is canon."""
    p = film / "qa/SIZE-LOCK.json"
    if not p.is_file():
        return ["qa/SIZE-LOCK.json required (film-level product scale canon)"]
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        return [f"SIZE-LOCK.json: {e}"]
    try:
        from .size_lock import validate_size_lock_data
    except ImportError:
        from size_lock import validate_size_lock_data
    return validate_size_lock_data(data, film, shots, require_still_hash=True)


def validate_stills(film: Path) -> list[str]:
    errs = []
    plan = film / "qa/REBEAT-PLAN.json"
    if not plan.is_file():
        return ["qa/REBEAT-PLAN.json required before stills"]
    shots = json.loads(plan.read_text()).get("shots") or []

    try:
        from .still_qa import (
            check_still_geometry,
            validate_product_lock_data,
            validate_still_sheet,
        )
        from .vlm_review import VLM_STILL_LOCK_NAME, validate_vlm_still_review_data
    except ImportError:
        from still_qa import (
            check_still_geometry,
            validate_product_lock_data,
            validate_still_sheet,
        )
        from vlm_review import VLM_STILL_LOCK_NAME, validate_vlm_still_review_data

    for s in shots:
        sid = s["id"]
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if not still.is_file():
            alt = film / "first-frames" / f"{sid}.jpg"
            if not alt.is_file():
                errs.append(f"missing still {sid}")
            else:
                errs.append(f"missing h3 still first-frames/h3/{sid}-first.jpg")
            continue
        geo_ok, _, geo_errs = check_still_geometry(still)
        if not geo_ok:
            errs.extend(geo_errs)

    errs.extend(validate_still_sheet(film, shots))

    prod_p = film / "qa/STILL-PRODUCT-LOCK.json"
    if not prod_p.is_file():
        errs.append("qa/STILL-PRODUCT-LOCK.json required (run: product-review)")
    else:
        try:
            errs.extend(validate_product_lock_data(json.loads(prod_p.read_text()), film, shots))
        except Exception as e:
            errs.append(f"STILL-PRODUCT-LOCK.json: {e}")

    vlm_p = film / VLM_STILL_LOCK_NAME
    if not vlm_p.is_file():
        errs.append(f"{VLM_STILL_LOCK_NAME} required (run: vlm-review-still)")
    else:
        try:
            errs.extend(validate_vlm_still_review_data(json.loads(vlm_p.read_text()), film, shots))
        except Exception as e:
            errs.append(f"VLM-STILL-REVIEW.json: {e}")

    errs.extend(validate_size_lock(film, shots))
    errs.extend(validate_person_lock(film, shots))
    return errs


def validate_person_lock(film: Path, shots: list) -> list[str]:
    """Same presenter across shots — match source continuity degree."""
    p = film / "qa/PERSON-LOCK.json"
    if not p.is_file():
        return [
            "qa/PERSON-LOCK.json required (same person/outfit across shots; "
            "canon usually S01 still + presenter-place.md)"
        ]
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        return [f"PERSON-LOCK.json: {e}"]
    try:
        from .person_lock import validate_person_lock_data
    except ImportError:
        from person_lock import validate_person_lock_data
    return validate_person_lock_data(data, film, shots)


def validate_assemble(film: Path) -> list[str]:
    try:
        from .assemble_tools import validate_assemble_data
    except ImportError:
        from assemble_tools import validate_assemble_data
    return validate_assemble_data(film)


def validate_h3(film: Path) -> list[str]:
    errs = []
    plan = film / "qa/REBEAT-PLAN.json"
    if not plan.is_file():
        return ["qa/REBEAT-PLAN.json required"]
    shots = json.loads(plan.read_text()).get("shots") or []
    meaning = film / "qa/H3-MEANING.md"
    structured = (film / "qa/H3-MEANING.json").is_file()
    meaning_txt = meaning.read_text() if meaning.is_file() else ""
    for s in shots:
        sid = s["id"]
        vid = film / "shots" / f"{sid}.mp4"
        if not vid.is_file():
            errs.append(f"missing shots/{sid}.mp4")
        if not structured and meaning.is_file() and not re.search(rf"\b{sid}\b[^\n]*\bPASS\b", meaning_txt):
            errs.append(f"{sid}: meaning PASS missing in qa/H3-MEANING.md")
    if structured:
        try:
            from .asr_verify import validate_meaning_lock_data
        except ImportError:
            from asr_verify import validate_meaning_lock_data
        errs.extend(validate_meaning_lock_data(film, shots))
    elif not meaning.is_file():
        errs.append("qa/H3-MEANING.json required (run asr-verify)")

    # Post-H3 visual QA (size stability, identity, no baked captions/tags, person continuity)
    vlock_p = film / "qa/H3-VISUAL-LOCK.json"
    if not vlock_p.is_file():
        errs.append("qa/H3-VISUAL-LOCK.json required (run inspect-shots + video-review)")
    else:
        try:
            vlock_data = json.loads(vlock_p.read_text())
            try:
                from .video_qa import validate_visual_lock_data
            except ImportError:
                from video_qa import validate_visual_lock_data
            errs.extend(validate_visual_lock_data(vlock_data, film, shots))
        except Exception as e:
            errs.append(f"H3-VISUAL-LOCK.json: {e}")

    try:
        from .h3_submit import _validate_task_inputs
        from .voice_lock import voice_requirements
        from .video_qa import CONTACT_DIR_NAME
    except ImportError:
        from h3_submit import _validate_task_inputs
        from voice_lock import voice_requirements
        from video_qa import CONTACT_DIR_NAME
    if structured:
        meaning_entries = read_strict_json(film / "qa/H3-MEANING.json").get("shots") or {}
        for row in shots:
            sid = row["id"]
            try:
                role, boot, bank = voice_requirements(film, sid)
                vid = film / "shots" / f"{sid}.mp4"
                task = read_strict_json(film / "tasks" / f"{sid}.task.json")
                sheet = film / CONTACT_DIR_NAME / f"{sid}.jpg"
                _validate_task_inputs(film, sid, task)
                if (task.get("state") != "DOWNLOADED" or task.get("acceptance") != "PASS" or task.get("sha256") != file_sha256(vid)
                        or task.get("role") != role or task.get("contact_sheet_sha256") != file_sha256(sheet)
                        or not task.get("acceptance_asr_sha256") or task["acceptance_asr_sha256"] != meaning_entries.get(sid, {}).get("asr_sha256")):
                    raise ValueError("missing or stale acceptance; run accept-shot")
                if role and (bank is None or (sid != boot and (task.get("mode") != "ref2v" or not task.get("voice_ref")))):
                    raise ValueError(f"{role} voice lock missing or shot did not use its voice bank")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errs.append(f"{sid}: {exc}")
    return errs


def refresh_locks(data: dict) -> None:
    stages = data["stages"]
    def passed(s: str) -> bool:
        return stages.get(s, {}).get("status") == "pass"

    data["locks"]["h3_submit"] = (
        passed("1_read_source")
        and passed("2_vo_adapt")
        and passed("3_beat_plan")
        and passed("4_stills")
    )
    data["locks"]["assemble"] = passed("5_h3")
    # Open only with user allow + assemble pass; stage 7 pass means superres already done.
    data["locks"]["superres"] = passed("6_assemble") and bool(
        data["locks"].get("superres_user_allow")
    )


def cmd_init(args: argparse.Namespace) -> int:
    batch = Path(args.batch) if args.batch else Path(__file__).resolve().parent.parent
    films = [Path(args.film)] if args.film else discover_films(batch)
    for film in films:
        if not film.is_dir():
            print("skip missing", film)
            continue
        p = film_pipeline(film)
        if p.exists() and not args.force:
            print("exists", p)
            continue
        data = blank_state(film.name)
        # If REBEAT already written, note it but do not auto-pass
        if (film / "qa/REBEAT-PLAN.json").is_file():
            data["stages"]["3_beat_plan"]["note"] = "REBEAT-PLAN.json present — run advance after source/vo pass"
        save(film, data)
        print("init", film.name)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    film = Path(args.film)
    data = load(film)
    refresh_locks(data)
    print(f"film: {data['film']}")
    print(f"current: {data['current']} — {STAGE_TITLE.get(data['current'], '')}")
    print(f"void.sparse7: {data.get('void', {}).get('sparse7_template')}")
    print("stages:")
    for s in STAGE_ORDER:
        st = data["stages"][s]
        mark = {"pass": "PASS", "fail": "FAIL", "pending": "....", "running": "RUN "}.get(st["status"], st["status"])
        print(f"  [{mark}] {s}  {STAGE_TITLE[s]}" + (f"  — {st['note']}" if st.get("note") else ""))
    print("locks:", json.dumps(data["locks"], ensure_ascii=False))
    ok, missing = artifacts_present(film, data["current"])
    if missing:
        print("current stage missing:", ", ".join(missing))
    else:
        print("current stage artifacts: OK")
    return 0


def _validate_stage(film: Path, stage: str, *, record: bool = False, state: dict | None = None) -> list[str]:
    try:
        errors = artifacts_present(film, stage)[1]
        if errors:
            return errors
        snapshot = {rel: file_sha256(film / rel) for rel in REQUIRED[stage]}
        if stage == "1_read_source":
            snapshot.update(_source_inputs(film))
        elif stage == "3_beat_plan":
            current = compile_script_contract(film, write=record)
            if not record:
                locked = read_strict_json(film / "qa/SCRIPT-LOCK.json")
                if locked["shots"] != current["shots"] or locked["inputs"] != current["inputs"]:
                    return ["SCRIPT-LOCK.json stale; re-pass 3_beat_plan"]
            snapshot["qa/SCRIPT-LOCK.json"] = file_sha256(film / "qa/SCRIPT-LOCK.json")
        elif stage == "5_h3" and not (film / "qa/H3-MEANING.json").is_file():
            return ["legacy meaning report is not hash-bound; run asr-verify for every shot"]
        elif stage == "5_h3":
            snapshot["qa/H3-MEANING.json"] = file_sha256(film / "qa/H3-MEANING.json")
        elif stage == "6_assemble":
            try:
                from .assemble_tools import validate_assemble_data
            except ImportError:
                from assemble_tools import validate_assemble_data
            errors = validate_assemble_data(film, strict=True)
            if errors:
                return errors
        if record and state is not None:
            state["inputs"] = snapshot
        elif not record and (state or {}).get("inputs") != snapshot:
            return [f"stage evidence changed or not locked; re-pass {stage}"]
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return [str(exc)]


def cmd_require(args: argparse.Namespace) -> int:
    film = Path(args.film).resolve()
    action = args.action
    if action not in ACTION_REQUIRES:
        return 3
    data = load(film)
    need = ACTION_REQUIRES[action]
    for stage in STAGE_ORDER[:STAGE_ORDER.index(need) + 1]:
        errors = ([f"{stage} is not pass"] if data["stages"][stage]["status"] != "pass"
                  else _validate_stage(film, stage, state=data["stages"][stage]))
        if errors:
            print(f"GATE FAIL: {stage}: " + "; ".join(errors), file=sys.stderr)
            return 2
    if action == "superres":
        try:
            allowed = read_strict_json(film / "qa/superres-allow.json")
            if allowed.get("allow") is not True or allowed.get("by") != "user":
                raise ValueError("superres not user-allowed")
        except ValueError as exc:
            print(f"GATE FAIL: {exc}", file=sys.stderr)
            return 2
    shot = getattr(args, "shot", None)
    if shot and action != "h3_submit":
        ids = {row["id"] for row in read_strict_json(film / "qa/REBEAT-PLAN.json")["shots"]}
        if shot not in ids:
            print(f"GATE FAIL: {shot} not in REBEAT-PLAN", file=sys.stderr)
            return 2
    if action == "h3_submit" and shot:
        try:
            from .asr_verify import validate_meaning_lock_data
            from .voice_lock import voice_requirements
        except ImportError:
            from asr_verify import validate_meaning_lock_data
            from voice_lock import voice_requirements
        try:
            role, bootstrap, bank = voice_requirements(film, shot)
            if role and bootstrap != shot:
                receipt = read_strict_json(film / "tasks" / f"{bootstrap}.task.json")
                if (bank is None or receipt.get("state") != "DOWNLOADED" or receipt.get("acceptance") != "PASS"
                        or receipt.get("sha256") != file_sha256(film / "shots" / f"{bootstrap}.mp4")
                        or validate_meaning_lock_data(film, [bootstrap])):
                    raise ValueError(f"{bootstrap}: accept {role} bootstrap shot before submitting {shot}")
            if not (film / "prompts" / f"{shot}.txt").is_file():
                raise ValueError(f"missing prompts/{shot}.txt")
        except (OSError, ValueError, TypeError) as exc:
            print(f"GATE FAIL: {exc}", file=sys.stderr)
            return 2
    print(json.dumps({"ok": True, "action": action, "film": film.name}, ensure_ascii=False))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    film = Path(args.film).resolve()
    stage = args.stage
    status = args.status
    if stage not in STAGE_ORDER:
        print("bad stage", file=sys.stderr)
        return 3
    if status not in {"pending", "running", "pass", "fail"}:
        print("bad status", file=sys.stderr)
        return 3
    data = load(film)
    evidence = {}
    old_evidence = data["stages"][stage].get("inputs")
    if status == "pass":
        for prior in STAGE_ORDER[:STAGE_ORDER.index(stage) + 1]:
            if prior != stage and data["stages"][prior]["status"] != "pass":
                print(f"cannot PASS {stage}; prior {prior} not pass", file=sys.stderr)
                return 2
            missing = _validate_stage(film, prior, record=prior == stage,
                                      state=evidence if prior == stage else data["stages"][prior])
            if missing:
                print(f"cannot PASS {stage}; {prior}: " + "; ".join(missing), file=sys.stderr)
                return 2
    data["stages"][stage] = {
        "status": status,
        "note": args.note or data["stages"][stage].get("note", ""),
        "updated": now(),
        **evidence,
    }
    # bump current
    if status == "pass":
        idx = STAGE_ORDER.index(stage)
        if old_evidence != evidence.get("inputs"):
            for later in STAGE_ORDER[idx + 1:]:
                if data["stages"][later]["status"] != "pending":
                    data["stages"][later] = {"status": "pending", "note": f"revalidate after {stage} changed", "updated": now()}
        if idx + 1 < len(STAGE_ORDER):
            data["current"] = STAGE_ORDER[idx + 1]
        else:
            data["current"] = stage
    elif status == "running":
        data["current"] = stage
    refresh_locks(data)
    save(film, data)
    print(json.dumps({"ok": True, "stage": stage, "status": status, "current": data["current"]}, ensure_ascii=False))
    return 0


def cmd_advance(args: argparse.Namespace) -> int:
    film = Path(args.film)
    data = load(film)
    stage = data["current"]
    args.stage = stage
    args.status = "pass"
    args.note = args.note or "advance"
    return cmd_set(args)


def cmd_gate_check(args: argparse.Namespace) -> int:
    film = Path(args.film)
    stage = args.stage
    ok, missing = artifacts_present(film, stage)
    print(json.dumps({"stage": stage, "ok": ok, "missing": missing}, ensure_ascii=False))
    return 0 if ok else 2


def cmd_void_sparse7(args: argparse.Namespace) -> int:
    film = Path(args.film)
    data = load(film)
    data["void"] = {
        "sparse7_template": True,
        "note": args.note or "Old 7-shot sparse VO/cut plan void — must rebeat via pipeline",
        "at": now(),
    }
    # Reset pipeline stages so nothing is falsely pass
    for s in STAGE_ORDER:
        data["stages"][s] = {"status": "pending", "note": "reset after void-sparse7", "updated": now()}
    data["current"] = STAGE_ORDER[0]
    data["locks"]["superres_user_allow"] = False
    refresh_locks(data)
    save(film, data)
    # stamp a marker file
    marker = film / "qa/VOID-SPARSE7.md"
    marker.write_text(
        f"# VOID sparse7\n\n{data['void']['note']}\n\nat: {data['void']['at']}\n"
        "Do not submit H3 from old 7-shot prompts. Follow REBEAT-PLAN + pipeline stages.\n"
    )
    print("voided", film.name)
    return 0


def cmd_allow_superres(args: argparse.Namespace) -> int:
    film = Path(args.film)
    data = load(film)
    data["locks"]["superres_user_allow"] = True
    allow = film / "qa/superres-allow.json"
    allow.write_text(json.dumps({"allow": True, "by": "user", "at": now()}, ensure_ascii=False, indent=2) + "\n")
    refresh_locks(data)
    save(film, data)
    print("superres allowed for", film.name)
    return 0


def cmd_extract_voice(args: argparse.Namespace) -> int:
    try:
        from .voice_lock import ensure_voice_lock, first_shot_id, shot_roles
        from .asr_verify import validate_meaning_lock_data
    except ImportError:
        from voice_lock import ensure_voice_lock, first_shot_id, shot_roles
        from asr_verify import validate_meaning_lock_data

    film = Path(args.film)
    role = getattr(args, "role", None)
    shot = getattr(args, "shot", None) or (next((sid for sid, value in shot_roles(film).items() if value == role), None) if role else first_shot_id(film))
    if shot not in shot_roles(film):
        raise ValueError("role/shot is not in the plan")
    task = read_strict_json(film / "tasks" / f"{shot}.task.json")
    if (task.get("acceptance") != "PASS" or task.get("state") != "DOWNLOADED"
            or task.get("sha256") != file_sha256(film / "shots" / f"{shot}.mp4")
            or validate_meaning_lock_data(film, [shot])):
        raise ValueError(f"{shot}: complete accept-shot before extracting a voice bank")
    meta = ensure_voice_lock(film, force=bool(args.force), role=role, shot=shot)
    print(json.dumps({"ok": True, **meta}, ensure_ascii=False))
    return 0


def cmd_size_review(args: argparse.Namespace) -> int:
    """Record structured size evidence for one shot (required under user_spec)."""
    try:
        from .size_lock import apply_size_review, write_size_lock
    except ImportError:
        from size_lock import apply_size_review, write_size_lock

    film = Path(args.film)
    path = film / "qa" / "SIZE-LOCK.json"
    if not path.is_file():
        print(f"SIZE-LOCK.json missing: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"SIZE-LOCK.json: {e}", file=sys.stderr)
        return 2

    refs = [r.strip() for r in (args.refs or "").split(",") if r.strip()]
    try:
        data = apply_size_review(
            data,
            args.shot,
            mode=args.mode,
            vs_user_spec=args.vs_spec,
            vs_source_occupancy=args.vs_source,
            refs_used=refs,
            note=args.note or "",
            status=args.status,
            film=film,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    write_size_lock(film, data)
    print(
        json.dumps(
            {"ok": True, "shot": args.shot, "entry": data["shots"][args.shot]},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_person_review(args: argparse.Namespace) -> int:
    """Record structured presenter continuity evidence for one shot."""
    try:
        from .person_lock import apply_person_review, write_person_lock
    except ImportError:
        from person_lock import apply_person_review, write_person_lock

    film = Path(args.film)
    path = film / "qa" / "PERSON-LOCK.json"
    if not path.is_file():
        print(f"PERSON-LOCK.json missing: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"PERSON-LOCK.json: {e}", file=sys.stderr)
        return 2

    try:
        data = apply_person_review(
            data,
            args.shot,
            same_person=not args.person_mismatch,
            same_outfit=not args.outfit_mismatch,
            same_place=not args.place_mismatch,
            note=args.note or "",
            status=args.status,
            film=film,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    write_person_lock(film, data)
    print(
        json.dumps(
            {"ok": True, "shot": args.shot, "entry": data["shots"][args.shot]},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_inspect_stills(args: argparse.Namespace) -> int:
    """Check still geometry/aspect ratios and build qa/still-contact-sheet.jpg."""
    try:
        from .still_qa import inspect_film_stills
    except ImportError:
        from still_qa import inspect_film_stills

    res = inspect_film_stills(Path(args.film))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 2


def cmd_product_review(args: argparse.Namespace) -> int:
    """Record structured pre-H3 product identity/geometry review bound to still_sha256."""
    try:
        from .still_qa import PRODUCT_LOCK_NAME, apply_product_review, build_product_lock
    except ImportError:
        from still_qa import PRODUCT_LOCK_NAME, apply_product_review, build_product_lock

    film = Path(args.film)
    path = film / PRODUCT_LOCK_NAME
    data = json.loads(path.read_text()) if path.is_file() else build_product_lock()
    try:
        data = apply_product_review(
            film,
            data,
            args.shot,
            features_intact=not args.features_fail,
            no_wrong_variant=not args.wrong_variant,
            no_hallucinated_tags=not args.hallucinated_tags,
            status=args.status,
            note=args.note or "",
        )
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ok": True, "shot": args.shot, "entry": data["shots"][args.shot]}, ensure_ascii=False))
    return 0


def _shot_ids_for_film(film: Path, shot_arg: str | None) -> list[str]:
    if shot_arg:
        return [shot_arg]
    plan = film / "qa" / "REBEAT-PLAN.json"
    if plan.is_file():
        return [s["id"] for s in (json.loads(plan.read_text()).get("shots") or [])]
    return sorted(p.stem.replace("-first", "") for p in (film / "first-frames" / "h3").glob("S*-first.jpg"))


def cmd_vlm_review_still(args: argparse.Namespace) -> int:
    """Run independent VLM review on pre-H3 first-frame still(s)."""
    try:
        from .vlm_review import review_still_with_vlm
    except ImportError:
        from vlm_review import review_still_with_vlm

    film = Path(args.film)
    sids = _shot_ids_for_film(film, args.shot)
    out = {}
    all_pass = True
    for sid in sids:
        entry = review_still_with_vlm(film, sid, provider=args.provider)
        out[sid] = entry
        if entry["verdict"] != "PASS":
            all_pass = False
    print(json.dumps({"ok": all_pass, "film": film.name, "shots": out}, ensure_ascii=False, indent=2))
    return 0 if all_pass else 2


def cmd_vlm_review_video(args: argparse.Namespace) -> int:
    """Run independent VLM review on post-H3 4-frame video contact sheet(s)."""
    try:
        from .vlm_review import review_video_with_vlm
    except ImportError:
        from vlm_review import review_video_with_vlm

    film = Path(args.film)
    sids = _shot_ids_for_film(film, args.shot)
    out = {}
    all_pass = True
    for sid in sids:
        entry = review_video_with_vlm(film, sid, provider=args.provider)
        out[sid] = entry
        if entry["status"] != "PASS":
            all_pass = False
    print(json.dumps({"ok": all_pass, "film": film.name, "shots": out}, ensure_ascii=False, indent=2))
    return 0 if all_pass else 2


def cmd_inspect_shots(args: argparse.Namespace) -> int:
    """Extract 4-frame contact sheets and scan audio glitches for H3 shots."""
    try:
        from .video_qa import inspect_film_shots
    except ImportError:
        from video_qa import inspect_film_shots

    film = Path(args.film)
    results = inspect_film_shots(film, shot_id=args.shot)
    print(json.dumps({"ok": True, "film": film.name, "shots": results}, ensure_ascii=False, indent=2))
    return 0


def cmd_video_review(args: argparse.Namespace) -> int:
    """Record post-H3 visual review bound to current shot_sha256."""
    try:
        from .video_qa import VISUAL_LOCK_NAME, apply_video_review, build_visual_lock
    except ImportError:
        from video_qa import VISUAL_LOCK_NAME, apply_video_review, build_visual_lock

    film = Path(args.film)
    path = film / VISUAL_LOCK_NAME
    if path.is_file():
        data = json.loads(path.read_text())
    else:
        data = build_visual_lock()

    try:
        data = apply_video_review(
            film,
            data,
            args.shot,
            size_stable=not args.size_drift,
            size_vs_spec=args.size_vs_spec,
            identity_intact=not args.identity_fail,
            no_baked_captions=not args.baked_captions,
            no_hallucinated_tags=not args.hallucinated_tags,
            person_consistent=not args.person_drift,
            status=args.status,
            note=args.note or "",
        )
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"ok": True, "shot": args.shot, "entry": data["shots"][args.shot]},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_audit_batch(args: argparse.Namespace) -> int:
    batch = Path(args.batch) if args.batch else Path(__file__).resolve().parent.parent
    rows = []
    for film in discover_films(batch):
        p = film_pipeline(film)
        if not p.is_file():
            rows.append((film.name, "NO_PIPELINE", "-", "-"))
            continue
        data = load(film)
        cur = data["current"]
        st = data["stages"][cur]["status"]
        rows.append((film.name, cur, st, "void7" if data.get("void", {}).get("sparse7_template") else ""))
    print(f"{'film':28} {'current':16} {'status':8} flag")
    for r in rows:
        print(f"{r[0]:28} {r[1]:16} {r[2]:8} {r[3]}")
    return 0


def cmd_source_prepare(args: argparse.Namespace) -> int:
    """Prepare stage-1 source artifacts (probe, optional BGM stem split, cuts, ASR, overview)."""
    try:
        from .hypit_bridge import prepare_source
    except ImportError:
        from hypit_bridge import prepare_source

    res = prepare_source(
        Path(args.film),
        Path(args.source),
        language=args.language,
        threshold=float(args.threshold),
        use_hypit=bool(args.use_hypit),
        transcribe=bool(args.transcribe),
        extract_bgm=bool(args.extract_bgm),
        bgm_engine=args.bgm_engine,
    )
    print(json.dumps({"ok": True, **res}, ensure_ascii=False, indent=2))
    return 0


def cmd_extract_bgm(args: argparse.Namespace) -> int:
    """Separate source vocals (qa/source-vocals.wav) and usable BGM (qa/bgm.wav)."""
    try:
        from .audio_stem import extract_source_stems
    except ImportError:
        from audio_stem import extract_source_stems

    try:
        res = extract_source_stems(
            Path(args.film),
            Path(args.source),
            engine=args.engine,
            model=args.model,
            force=bool(args.force),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **res}, ensure_ascii=False, indent=2))
    return 0


def cmd_lint_prompts(args: argparse.Namespace) -> int:
    """Lint H3 prompts for spoken duration fit, complete clauses, tags, and brief claims."""
    try:
        from .script_lint import lint_film_prompts
    except ImportError:
        from script_lint import lint_film_prompts

    res = lint_film_prompts(Path(args.film), shot_id=args.shot, pace=args.pace)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 2


def cmd_estimate_speech(args: argparse.Namespace) -> int:
    """Estimate spoken seconds for Chinese/mixed copy before locking shot duration."""
    try:
        from .script_lint import estimate_with_hypit, estimate_zh_speech
    except ImportError:
        from script_lint import estimate_with_hypit, estimate_zh_speech

    text = Path(args.file).read_text() if args.file else (args.text or "")
    if not text.strip():
        print("Provide --text or --file", file=sys.stderr)
        return 3
    if args.use_hypit:
        res = estimate_with_hypit(text, language=args.language, pace=args.pace)
    else:
        res = estimate_zh_speech(text, pace=args.pace, padding=float(args.padding))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_candidate_list(args: argparse.Namespace) -> int:
    """List archived H3 attempt candidates for a film or one shot."""
    try:
        from .candidates import list_candidates
    except ImportError:
        from candidates import list_candidates

    print(json.dumps(list_candidates(Path(args.film), shot=args.shot), ensure_ascii=False, indent=2))
    return 0


def cmd_candidate_archive(args: argparse.Namespace) -> int:
    """Archive current shot video/task into shots/candidates/ before rerolling."""
    try:
        from .candidates import archive_current_shot
    except ImportError:
        from candidates import archive_current_shot

    entry = archive_current_shot(
        Path(args.film),
        args.shot,
        note=args.note or "",
        clear_active=bool(args.clear),
    )
    if entry is None:
        print(f"Nothing to archive for {args.shot}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "candidate": entry}, ensure_ascii=False, indent=2))
    return 0


def cmd_candidate_select(args: argparse.Namespace) -> int:
    """Restore a preserved H3 candidate into active shots/ and contact sheets."""
    try:
        from .candidates import select_candidate
    except ImportError:
        from candidates import select_candidate

    try:
        res = select_candidate(Path(args.film), args.shot, args.candidate)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    """Assemble trimmed shots with ducked BGM and ASR-aligned Douyin captions."""
    try:
        from .assemble_tools import assemble_film
    except ImportError:
        from assemble_tools import assemble_film

    try:
        res = assemble_film(
            Path(args.film),
            bgm_path=Path(args.bgm) if args.bgm else None,
            bgm_source=args.bgm_source,
            bgm_db=float(args.bgm_db) if args.bgm_db is not None else None,
            asr_dir=Path(args.asr_dir) if args.asr_dir else None,
            width=int(args.width),
            height=int(args.height),
            skip_gate=bool(args.skip_gate),
            allow_uncaptioned=bool(args.allow_uncaptioned),
            matrix_seed=getattr(args, "matrix_seed", None),
            differentiate=getattr(args, "differentiate", False),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_review_html(args: argparse.Namespace) -> int:
    """Generate a Hypit Studio-inspired interactive HTML review & feedback page."""
    try:
        from .review_gallery import write_review_html
    except ImportError:
        from review_gallery import write_review_html

    film = Path(args.film) if args.film else None
    batch = Path(args.batch) if args.batch else (None if film else Path.cwd())
    out = Path(args.out) if args.out else None
    p = write_review_html(film=film, batch=batch, out=out)
    print(json.dumps({"ok": True, "review_html": str(p)}, ensure_ascii=False))
    return 0


def cmd_export_hypit(args: argparse.Namespace) -> int:
    """Export film shots, script, timeline, and BGM as an editable Hypit SVML/SVRun project."""
    try:
        from .hypit_bridge import export_hypit_project
    except ImportError:
        from hypit_bridge import export_hypit_project

    try:
        res = export_hypit_project(
            Path(args.film),
            out_dir=Path(args.out) if args.out else None,
            width=int(args.width),
            height=int(args.height),
            fps=int(args.fps),
        )
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    return cmd_require(argparse.Namespace(film=args.film, action={"h3": "h3_submit"}.get(args.before, args.before), shot=args.shot))


def cmd_asr_verify(args: argparse.Namespace) -> int:
    try:
        from .asr_verify import verify_shot_meaning
    except ImportError:
        from asr_verify import verify_shot_meaning
    result = verify_shot_meaning(Path(args.film), args.shot, asr_path=args.asr_path, language=args.language)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "PASS" else 2


def cmd_accept_shot(args: argparse.Namespace) -> int:
    try:
        from .h3_submit import accept_downloaded_shot
    except ImportError:
        from h3_submit import accept_downloaded_shot
    result = accept_downloaded_shot(Path(args.film), args.shot, vlm_provider=args.vlm_provider, asr_path=args.asr_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_audit_matrix(args: argparse.Namespace) -> int:
    try:
        from .matrix_audit import audit_matrix
    except ImportError:
        from matrix_audit import audit_matrix
    result = audit_matrix(args.batch, out_dir=args.out, policy_path=args.policy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def cmd_superres(args: argparse.Namespace) -> int:
    try:
        from .superres import run_superres
    except ImportError:
        from superres import run_superres
    result = run_superres(Path(args.film), base_url=args.base_url, resume=args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Replica fail-closed pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--batch")
    p.add_argument("--film")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status")
    p.add_argument("film")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("require")
    p.add_argument("film")
    p.add_argument("action", choices=list(ACTION_REQUIRES))
    p.add_argument("--shot")
    p.set_defaults(func=cmd_require)

    p = sub.add_parser("preflight")
    p.add_argument("film")
    p.add_argument("--before", required=True, choices=("stills", "h3", "assemble"))
    p.add_argument("--shot")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("asr-verify")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--asr-path")
    p.add_argument("--language", default="zh")
    p.set_defaults(func=cmd_asr_verify)

    p = sub.add_parser("accept-shot")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--asr-path")
    p.add_argument("--vlm-provider", choices=("none", "auto", "codex", "openai"), default="none")
    p.set_defaults(func=cmd_accept_shot)

    p = sub.add_parser("audit-matrix")
    p.add_argument("--batch", action="append", required=True)
    p.add_argument("--policy")
    p.add_argument("--out")
    p.set_defaults(func=cmd_audit_matrix)

    p = sub.add_parser("superres")
    p.add_argument("film")
    p.add_argument("--base-url")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_superres)

    p = sub.add_parser("set")
    p.add_argument("film")
    p.add_argument("stage", choices=STAGE_ORDER)
    p.add_argument("status", choices=["pending", "running", "pass", "fail"])
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("advance")
    p.add_argument("film")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("gate-check")
    p.add_argument("film")
    p.add_argument("stage", choices=STAGE_ORDER)
    p.set_defaults(func=cmd_gate_check)

    p = sub.add_parser("void-sparse7")
    p.add_argument("film")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_void_sparse7)

    p = sub.add_parser("allow-superres")
    p.add_argument("film")
    p.set_defaults(func=cmd_allow_superres)

    p = sub.add_parser("extract-voice")
    p.add_argument("film")
    p.add_argument("--role")
    p.add_argument("--shot")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_extract_voice)

    p = sub.add_parser("size-review")
    p.add_argument("film")
    p.add_argument("--shot", required=True, help="Shot id, e.g. S01")
    p.add_argument(
        "--mode",
        required=True,
        choices=["worn", "handheld", "open", "detail", "other"],
        help="How the product sits in this still",
    )
    p.add_argument(
        "--vs-source",
        dest="vs_source",
        default="smaller",
        help="vs source product occupancy: smaller|n/a (same/larger forbidden)",
    )
    p.add_argument(
        "--vs-spec",
        dest="vs_spec",
        default="match",
        help="Must be match — still follows user physical_cm / worn_rule",
    )
    p.add_argument(
        "--refs",
        default="",
        help="Comma list, must include size diagram (e.g. front-with-size.jpg)",
    )
    p.add_argument("--note", default="")
    p.add_argument("--status", default="PASS", choices=["PASS", "FAIL"])
    p.set_defaults(func=cmd_size_review)

    p = sub.add_parser("person-review")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--person-mismatch", action="store_true")
    p.add_argument("--outfit-mismatch", action="store_true")
    p.add_argument("--place-mismatch", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--status", default="PASS", choices=["PASS", "FAIL"])
    p.set_defaults(func=cmd_person_review)

    p = sub.add_parser("inspect-stills")
    p.add_argument("film")
    p.set_defaults(func=cmd_inspect_stills)

    p = sub.add_parser("product-review")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--features-fail", action="store_true")
    p.add_argument("--wrong-variant", action="store_true")
    p.add_argument("--hallucinated-tags", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--status", default="PASS", choices=["PASS", "FAIL"])
    p.set_defaults(func=cmd_product_review)

    p = sub.add_parser("vlm-review-still")
    p.add_argument("film")
    p.add_argument("--shot", default=None)
    p.add_argument("--provider", default="auto", choices=["auto", "codex", "openai"])
    p.set_defaults(func=cmd_vlm_review_still)

    p = sub.add_parser("vlm-review-video")
    p.add_argument("film")
    p.add_argument("--shot", default=None)
    p.add_argument("--provider", default="auto", choices=["auto", "codex", "openai"])
    p.set_defaults(func=cmd_vlm_review_video)

    p = sub.add_parser("inspect-shots")
    p.add_argument("film")
    p.add_argument("--shot", default=None)
    p.set_defaults(func=cmd_inspect_shots)

    p = sub.add_parser("video-review")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--size-vs-spec", default="match", help="match|too_big|too_small|drift")
    p.add_argument("--size-drift", action="store_true")
    p.add_argument("--identity-fail", action="store_true")
    p.add_argument("--baked-captions", action="store_true")
    p.add_argument("--hallucinated-tags", action="store_true")
    p.add_argument("--person-drift", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--status", default="PASS", choices=["PASS", "FAIL"])
    p.set_defaults(func=cmd_video_review)

    p = sub.add_parser("audit-batch")
    p.add_argument("--batch")
    p.set_defaults(func=cmd_audit_batch)

    p = sub.add_parser("source-prepare")
    p.add_argument("film")
    p.add_argument("--source", required=True)
    p.add_argument("--language", default="zh")
    p.add_argument("--threshold", default="0.35")
    p.add_argument("--use-hypit", action="store_true")
    p.add_argument("--transcribe", action="store_true")
    p.add_argument("--extract-bgm", action="store_true")
    p.add_argument("--bgm-engine", default="auto", choices=["auto", "demucs", "audio-separator", "center-cancel"])
    p.set_defaults(func=cmd_source_prepare)

    p = sub.add_parser("extract-bgm")
    p.add_argument("film")
    p.add_argument("--source", required=True)
    p.add_argument("--engine", default="auto", choices=["auto", "demucs", "audio-separator", "center-cancel"])
    p.add_argument("--model", default="htdemucs")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_extract_bgm)

    p = sub.add_parser("lint-prompts")
    p.add_argument("film")
    p.add_argument("--shot", default=None)
    p.add_argument("--pace", default="normal", choices=["slow", "normal", "fast"])
    p.set_defaults(func=cmd_lint_prompts)

    p = sub.add_parser("estimate-speech")
    p.add_argument("--text", default="")
    p.add_argument("--file", default=None)
    p.add_argument("--language", default="zh")
    p.add_argument("--pace", default="normal", choices=["slow", "normal", "fast"])
    p.add_argument("--padding", default="0")
    p.add_argument("--use-hypit", action="store_true")
    p.set_defaults(func=cmd_estimate_speech)

    p = sub.add_parser("candidate-list")
    p.add_argument("film")
    p.add_argument("--shot", default=None)
    p.set_defaults(func=cmd_candidate_list)

    p = sub.add_parser("candidate-archive")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_candidate_archive)

    p = sub.add_parser("candidate-select")
    p.add_argument("film")
    p.add_argument("--shot", required=True)
    p.add_argument("--candidate", required=True)
    p.set_defaults(func=cmd_candidate_select)

    p = sub.add_parser("assemble")
    p.add_argument("film")
    p.add_argument("--bgm", default=None)
    p.add_argument("--bgm-source", default="none", choices=["source", "new", "none"])
    p.add_argument("--bgm-db", default=None)
    p.add_argument("--asr-dir", default=None)
    p.add_argument("--width", default="1152")
    p.add_argument("--height", default="2048")
    p.add_argument("--skip-gate", action="store_true")
    p.add_argument("--allow-uncaptioned", action="store_true")
    p.add_argument("--matrix-seed")
    p.add_argument("--differentiate", action="store_true")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("review-html")
    p.add_argument("--film", default=None)
    p.add_argument("--batch", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_review_html)

    p = sub.add_parser("export-hypit")
    p.add_argument("film")
    p.add_argument("--out", default=None)
    p.add_argument("--width", default="1080")
    p.add_argument("--height", default="1920")
    p.add_argument("--fps", default="30")
    p.set_defaults(func=cmd_export_hypit)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
