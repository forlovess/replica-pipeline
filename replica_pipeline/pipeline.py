#!/usr/bin/env python3
"""Fail-closed replica pipeline CLI.

Usage:
  python -m replica_pipeline.pipeline init [--batch DIR] [--film DIR]
  python -m replica_pipeline.pipeline status FILM_DIR
  python -m replica_pipeline.pipeline require FILM_DIR ACTION
  python -m replica_pipeline.pipeline set FILM_DIR STAGE STATUS [--note TEXT]
  python -m replica_pipeline.pipeline advance FILM_DIR
  python -m replica_pipeline.pipeline gate-check FILM_DIR STAGE
  python -m replica_pipeline.pipeline audit-batch [BATCH_DIR]
  python -m replica_pipeline.pipeline void-sparse7 FILM_DIR
  python -m replica_pipeline.pipeline allow-superres FILM_DIR

Exit codes: 0 ok, 2 gate fail, 3 bad args.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    from replica_pipeline.stages import (
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
    if stage == "2_vo_adapt":
        missing.extend(validate_vo_density(film))
    if stage == "7_superres":
        allow = film / "qa/superres-allow.json"
        if allow.is_file():
            try:
                if not json.loads(allow.read_text()).get("allow"):
                    missing.append("qa/superres-allow.json:allow!=true")
            except Exception:
                missing.append("qa/superres-allow.json:invalid")
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
    if not plan.get("source_basis") and not any(s.get("source") for s in shots):
        errs.append("REBEAT-PLAN: set source_basis=scene_merge|vo_beat")
    return errs


def validate_vo_density(film: Path) -> list[str]:
    p = film / "qa/vo-density.json"
    if not p.is_file():
        return []
    errs = []
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        return [f"vo-density.json: {e}"]
    src = d.get("source_chars") or 0
    rem = d.get("remake_chars") or 0
    if src <= 0:
        return errs
    ratio = rem / src if src else 0
    if d.get("ratio") is None:
        errs.append("vo-density.json: missing ratio")
    if not d.get("facts_exhausted") and ratio < 0.8:
        errs.append(
            f"vo-density: remake/source={ratio:.2f} < 0.80 and facts_exhausted!=true "
            "(collapse into short lines forbidden)"
        )
    if d.get("looped_claims"):
        errs.append("vo-density: looped_claims=true forbidden")
    return errs


def validate_stills(film: Path) -> list[str]:
    errs = []
    plan = film / "qa/REBEAT-PLAN.json"
    if not plan.is_file():
        return ["qa/REBEAT-PLAN.json required before stills"]
    shots = json.loads(plan.read_text()).get("shots") or []
    lock = film / "qa/STILL-PRODUCT-LOCK.md"
    lock_txt = lock.read_text() if lock.is_file() else ""
    for s in shots:
        sid = s["id"]
        still = film / "first-frames" / "h3" / f"{sid}-first.jpg"
        if not still.is_file():
            alt = film / "first-frames" / f"{sid}.jpg"
            if not alt.is_file():
                errs.append(f"missing still {sid}")
            else:
                errs.append(f"missing h3 still first-frames/h3/{sid}-first.jpg")
        if lock_txt and sid in lock_txt:
            if not re.search(rf"{sid}[^\n]*PASS", lock_txt) and not re.search(
                rf"\|\s*{sid}\s*\|[^\n]*PASS", lock_txt
            ):
                if not re.search(rf"{sid}.*PASS|PASS.*{sid}|全部.*PASS|S0\d–S0\d.*PASS", lock_txt):
                    errs.append(f"{sid}: not marked PASS in STILL-PRODUCT-LOCK.md")
        elif lock.is_file():
            errs.append(f"{sid}: not found in STILL-PRODUCT-LOCK.md")
    return errs


def validate_h3(film: Path) -> list[str]:
    errs = []
    plan = film / "qa/REBEAT-PLAN.json"
    if not plan.is_file():
        return ["qa/REBEAT-PLAN.json required"]
    shots = json.loads(plan.read_text()).get("shots") or []
    meaning = film / "qa/H3-MEANING.md"
    meaning_txt = meaning.read_text() if meaning.is_file() else ""
    for s in shots:
        sid = s["id"]
        vid = film / "shots" / f"{sid}.mp4"
        if not vid.is_file():
            errs.append(f"missing shots/{sid}.mp4")
        if meaning.is_file() and not re.search(rf"{sid}[^\n]*PASS", meaning_txt):
            errs.append(f"{sid}: meaning PASS missing in qa/H3-MEANING.md")
    if not meaning.is_file():
        errs.append("qa/H3-MEANING.md required")
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
    data["locks"]["superres"] = passed("6_assemble") and bool(
        data["locks"].get("superres_user_allow")
    )


def cmd_init(args: argparse.Namespace) -> int:
    batch = Path(args.batch) if args.batch else Path.cwd()
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


def cmd_require(args: argparse.Namespace) -> int:
    film = Path(args.film)
    action = args.action
    if action not in ACTION_REQUIRES:
        print(f"unknown action {action}", file=sys.stderr)
        return 3
    data = load(film)
    refresh_locks(data)
    save(film, data)

    need = ACTION_REQUIRES[action]
    for s in STAGE_ORDER:
        if data["stages"][s]["status"] != "pass":
            print(
                f"GATE FAIL: action={action} blocked; stage {s} is {data['stages'][s]['status']} "
                f"(requires through {need})",
                file=sys.stderr,
            )
            return 2
        if s == need:
            break

    if action == "h3_submit" and not data["locks"].get("h3_submit"):
        print("GATE FAIL: locks.h3_submit=false", file=sys.stderr)
        return 2
    if action == "assemble" and not data["locks"].get("assemble"):
        print("GATE FAIL: locks.assemble=false", file=sys.stderr)
        return 2
    if action == "superres":
        if not data["locks"].get("superres_user_allow"):
            print("GATE FAIL: superres not user-allowed", file=sys.stderr)
            return 2
        if data["stages"]["6_assemble"]["status"] != "pass":
            print("GATE FAIL: assemble not pass", file=sys.stderr)
            return 2

    if action == "h3_submit" and args.shot:
        shot = args.shot
        still = film / "first-frames" / "h3" / f"{shot}-first.jpg"
        if not still.is_file():
            print(f"GATE FAIL: missing {still}", file=sys.stderr)
            return 2
        plan = film / "qa/REBEAT-PLAN.json"
        if plan.is_file():
            ids = {s["id"] for s in json.loads(plan.read_text()).get("shots") or []}
            if shot not in ids:
                print(f"GATE FAIL: {shot} not in REBEAT-PLAN.json", file=sys.stderr)
                return 2

    print(json.dumps({"ok": True, "action": action, "film": film.name}, ensure_ascii=False))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    film = Path(args.film)
    stage = args.stage
    status = args.status
    if stage not in STAGE_ORDER:
        print("bad stage", file=sys.stderr)
        return 3
    if status not in {"pending", "running", "pass", "fail"}:
        print("bad status", file=sys.stderr)
        return 3
    data = load(film)
    if status == "pass":
        ok, missing = artifacts_present(film, stage)
        if not ok:
            print("cannot PASS; missing:", ", ".join(missing), file=sys.stderr)
            return 2
        for s in STAGE_ORDER:
            if s == stage:
                break
            if data["stages"][s]["status"] != "pass":
                print(f"cannot PASS {stage}; prior {s} not pass", file=sys.stderr)
                return 2
    data["stages"][stage] = {
        "status": status,
        "note": args.note or data["stages"][stage].get("note", ""),
        "updated": now(),
    }
    if status == "pass":
        idx = STAGE_ORDER.index(stage)
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
    ok, missing = artifacts_present(film, stage)
    if not ok:
        print(f"advance blocked at {stage}; missing: {', '.join(missing)}", file=sys.stderr)
        return 2
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
    for s in STAGE_ORDER:
        data["stages"][s] = {"status": "pending", "note": "reset after void-sparse7", "updated": now()}
    data["current"] = STAGE_ORDER[0]
    data["locks"]["superres_user_allow"] = False
    refresh_locks(data)
    save(film, data)
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


def cmd_audit_batch(args: argparse.Namespace) -> int:
    batch = Path(args.batch) if args.batch else Path.cwd()
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

    p = sub.add_parser("audit-batch")
    p.add_argument("--batch")
    p.set_defaults(func=cmd_audit_batch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
