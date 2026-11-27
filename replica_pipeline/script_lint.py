"""Script, dialogue, and prompt pre-flight linter (inspired by Hypit Script craft).

Validates single verbal authority across VO-REMAKE, REBEAT-PLAN, and H3 prompts
before spending generation credits:
- Estimates spoken duration in Chinese/mixed text (`slow`/`normal`/`fast` paces),
  matching Hypit's `estimate` approach and delegating to `hypit estimate` when wanted.
- Resolves Dual Text (`<display|spoken>`) so spoken checks measure pronunciation.
- Flags truncated/dangling spoken tails (e.g. "再一个就是") and broken `<d>` tags.
- Lints prompts for `<Picture 1>`, `<Audio 1>` (S02+), duration overrun, and banned
  claims declared in `qa/BRIEF-RULES.json`.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

PACE_RATES_ZH = {
    "slow": 3.2,
    "normal": 4.4,
    "fast": 5.6,
}

# Incomplete sentence tails that indicate careless mid-thought truncation.
DANGLING_TAILS = (
    "再一个就是",
    "还有就是",
    "而且这个",
    "但是这个",
    "不过这个",
    "因为这个",
    "所以这个",
    "也就是说",
    "比如这个",
    "然后就是",
    "其实这个",
    "另外这个",
)

DANGLING_END_WORDS = ("因为", "所以", "但是", "不过", "而且", "并且", "或者", "如果", "就是")

DIALOGUE_RE = re.compile(r"<d>(.*?)</d>", re.DOTALL | re.IGNORECASE)
DUAL_TEXT_RE = re.compile(r"<([^<>|]+)\|([^<>]*)>")
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")
TAG_PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
ROLE_RE = re.compile(r"<([A-Z][A-Z0-9_]{0,31})>")
SHOT_RE = re.compile(r"S(?:0[1-9]|[1-9][0-9]+)")
PICTURE_TAG_RE = re.compile(r"<\s*Picture\s+1\s*>", re.IGNORECASE)
AUDIO_TAG_RE = re.compile(r"<\s*Audio\s+1\s*>", re.IGNORECASE)

# Phrases that forbid subtitles/tags are fine; bare calls to add on-screen captions are not.
NEGATED_SUBTITLE_RE = re.compile(
    r"(no\s+(?:burned[- ]in\s+)?(?:captions?|subtitles?|text\s+overlays?|price\s+tags?)|"
    r"without\s+(?:captions?|subtitles?)|"
    r"无字幕|不要字幕|禁字幕)",
    re.IGNORECASE,
)
REQUESTED_SUBTITLE_RE = re.compile(
    r"(add\s+(?:big\s+)?(?:captions?|subtitles?)|with\s+on-screen\s+subtitles?|"
    r"加字幕|带字幕|配字幕|贴价格牌)",
    re.IGNORECASE,
)


def project_dual_text(text: str, *, side: str = "spoken") -> str:
    """Resolve Hypit Dual Text `<display|spoken>` to either 'spoken' or 'display'."""
    def _repl(m: re.Match[str]) -> str:
        disp, spk = m.group(1).strip(), m.group(2).strip()
        if side == "display":
            return disp
        return spk if spk else disp

    return DUAL_TEXT_RE.sub(_repl, text)


def compile_shot_text(raw_text: str, *, default_role: str = "HOST") -> dict[str, Any]:
    if not isinstance(raw_text, str):
        raise ValueError("shot text must be a string")
    role = default_role
    roles = set(ROLE_RE.findall(raw_text))
    if len(roles) > 1 or not isinstance(role, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", role):
        raise ValueError("invalid or conflicting role cues")
    if roles:
        role = roles.pop()
    clean = ROLE_RE.sub("", raw_text)
    tags = re.findall(r"</?d>", clean, re.I)
    if [tag.lower() for tag in tags] != ["<d>", "</d>"] * len(DIALOGUE_RE.findall(clean)):
        raise ValueError("unbalanced <d> dialogue tags")
    leftovers = DUAL_TEXT_RE.sub("", clean)
    if re.search(r"<[^<>\n]*\||\|[^<>\n]*>|</?[A-Z][A-Z0-9_]*\b(?![^<>]*>)|</[A-Z][A-Z0-9_]*>|</?d(?=\s|$)", leftovers):
        raise ValueError("malformed dual text or role/dialogue tag")
    phrases = []
    for segment in DIALOGUE_RE.findall(clean) if tags else clean.splitlines():
        segment = TAG_PREFIX_RE.sub("", segment).strip()
        if not segment:
            continue
        for match in DUAL_TEXT_RE.finditer(segment):
            if not any(part.strip() for part in match.groups()) or "|" in match[2]:
                raise ValueError("dual text requires exactly one display and spoken value")
        phrases.append({side: project_dual_text(segment, side=side) for side in ("display", "spoken")})
    return {"raw": raw_text, "role": role, "phrases": phrases,
            **{side: " ".join(part[side] for part in phrases) for side in ("display", "spoken")}}


def speech_chars(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def spoken_signature(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?|[a-z]+(?:'[a-z]+)?|[\u3400-\u9fff]", unicodedata.normalize("NFKC", text).casefold())


def read_strict_json(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"invalid constant {value}")

    try:
        data = json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=reject_constant)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        return data
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def lint_hook(shot: dict, *, rules: dict | None = None) -> list[str]:
    rules = rules or {}
    text = compile_shot_text(shot.get("spoken") or shot.get("vo") or "")["spoken"]
    start = shot.get("hook_start_sec", shot.get("hook_start", 0))
    end = shot.get("hook_end_sec", shot.get("hook_end"))
    silent = shot.get("silent") is True
    hook = shot.get("hook") or re.split(r"[，。！？,.!?]", text, maxsplit=1)[0]
    errors = []
    if not isinstance(hook, str):
        return ["S01: hook must be text"]
    if not speech_chars(hook) or (not silent and not speech_chars(text).startswith(speech_chars(hook))):
        errors.append("S01: missing or contradictory opening hook")
    if (type(start) not in (int, float) or not 0 <= start < 3 or
            (end is not None and (type(end) not in (int, float) or not start < end <= 3)) or
            (silent and end is None) or
            (not silent and end is None and isinstance(start, (int, float)) and start + count_speech_units(hook) / PACE_RATES_ZH["normal"] > 3)):
        errors.append("S01: opening hook must land within 3s")
    if re.match(r"(?:大家好|哈喽大家好|今天给大家推荐|Hello\b|Hi everyone\b)", text, re.I):
        allowed = shot if "allow_generic_hook" in shot else rules
        reason = allowed.get("generic_hook_reason") or allowed.get("allow_generic_hook_reason") or allowed.get("hook_reason") or allowed.get("reason")
        if allowed.get("allow_generic_hook") is not True or not isinstance(reason, str) or not reason.strip():
            errors.append("S01: generic opening requires allow_generic_hook=true and a reason")
    return errors


def plan_visual_durations(shots: list[dict], fallback_durations: dict[str, float]) -> dict[str, float]:
    durations = {}
    for index, row in enumerate(shots):
        sid = row["id"]
        duration = row.get("visual_sec", row.get("sec", fallback_durations[sid]))
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"{sid}: invalid visual duration")
        if index + 1 < len(shots) and not {"visual_sec", "audio_out"} & row.keys():
            following = shots[index + 1]
            cut = following.get("audio_in")
            if following.get("audio_owner") == sid and type(cut) in (int, float) and 0 < cut < duration:
                if type(row.get("audio_in", 0)) not in (int, float):
                    raise ValueError(f"{sid}: invalid audio_in")
                duration = cut - row.get("audio_in", 0)
        if duration <= 0:
            raise ValueError(f"{sid}: visual cut precedes audio start")
        durations[sid] = float(duration)
    return durations


def audio_windows(shots: list[dict], durations: dict[str, float], *, source_durations: dict[str, float] | None = None) -> list[tuple[str, float, float]]:
    windows = []
    owners = {}
    ends = {}
    silent = {row["id"] for row in shots if row.get("silent")}
    source_durations = source_durations or {row["id"]: row.get("audio_source_sec", durations[row["id"]]) for row in shots}
    for row in shots:
        sid = row["id"]
        owner = row.get("audio_owner", sid)
        if not isinstance(owner, str) or (owner != sid and (owner not in owners or owners[owner] != owner)):
            raise ValueError(f"{sid}: audio_owner must be an earlier direct owner")
        if owner != sid and not {"audio_in", "audio_out"} <= row.keys():
            raise ValueError(f"{sid}: dependent audio requires audio_in and audio_out")
        if owner != sid and owner in silent:
            raise ValueError(f"{sid}: audio_owner {owner} is silent")
        start = row.get("audio_in", 0)
        end = row.get("audio_out", start + durations[sid] if type(start) in (int, float) else None)
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or type(source_durations[owner]) not in (int, float) or not 0 <= start < end <= source_durations[owner] + 0.01
                or (owner != sid and abs(end - start - durations[sid]) > 0.1)):
            raise ValueError(f"{sid}: out-of-range audio span or span does not match visual duration")
        if start + 0.001 < ends.get(owner, 0):
            raise ValueError(f"{sid}: overlaps or reverses an earlier {owner} audio span")
        ends[owner] = end
        owners[sid] = owner
        windows.append((owner, float(start), float(end)))
    return windows


def vo_remake_text(text: str) -> str:
    if re.search(r"</?d>", text, re.I):
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "```"))]
    per_shot = [match[1] for line in lines if (match := re.match(r"(?:[-*]\s*)?\**S\d+\**\s*[:：|]\s*(.+)", line))]
    return "\n".join(per_shot or lines)


def compile_remake_text(text: str) -> str:
    return compile_shot_text(ROLE_RE.sub("", vo_remake_text(text)))["spoken"]


def compile_script_contract(film: Path, *, write: bool = True) -> dict:
    film = Path(film)
    plan_path = film / "qa/REBEAT-PLAN.json"
    vo_path = film / "qa/VO-REMAKE.md"
    plan = read_strict_json(plan_path)
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots or not all(isinstance(row, dict) for row in shots):
        raise ValueError("REBEAT-PLAN: expected nonempty shots array")
    ids = [row.get("id") for row in shots]
    if not all(isinstance(sid, str) and SHOT_RE.fullmatch(sid) for sid in ids) or len(set(ids)) != len(ids) or ids[0] != "S01":
        raise ValueError("REBEAT-PLAN: unique valid shot IDs starting at S01 required")
    if any(type(row.get("sec")) not in (int, float) or not 4 <= row["sec"] <= 15 for row in shots):
        raise ValueError("REBEAT-PLAN: shot sec must be in [4, 15]")
    for row in shots:
        for key in ("audio_source_sec", "visual_sec"):
            minimum = 4 if key == "audio_source_sec" else 0.001
            if key in row and (type(row[key]) not in (int, float) or not minimum <= row[key] <= 15):
                raise ValueError(f"{row['id']}: invalid {key}")
        if row.get("audio_source_sec", row["sec"]) < row.get("visual_sec", row["sec"]):
            raise ValueError(f"{row['id']}: source duration shorter than its visual window")
    source_durations = {row["id"]: row.get("audio_source_sec", row["sec"]) for row in shots}
    windows = audio_windows(shots, plan_visual_durations(shots, source_durations), source_durations=source_durations)
    rules = load_brief_rules(film)
    errors = lint_hook(shots[0], rules={**rules, **plan})
    result = {"shots": {}, "inputs": {}}
    paths = [plan_path, vo_path, *brief_rule_paths(film)]
    for row, (owner, _, _) in zip(shots, windows):
        sid = row["id"]
        raw = row.get("spoken") or row.get("vo") or ""
        prompt = film / "prompts" / f"{sid}.txt"
        text = prompt.read_text() if prompt.is_file() else ""
        prompt_roles = set(ROLE_RE.findall(text))
        default_role = row.get("role", next(iter(prompt_roles)) if len(prompt_roles) == 1 else "HOST")
        compiled = compile_shot_text(raw, default_role=default_role)
        if row.get("role", compiled["role"]) != compiled["role"]:
            errors.append(f"{sid}: role contradicts script cue")
        spoken = compiled["spoken"]
        if "silent" in row and type(row["silent"]) is not bool:
            errors.append(f"{sid}: silent must be a boolean")
        if row.get("spoken") and row.get("vo") and spoken_signature(compile_shot_text(row["vo"])["spoken"]) != spoken_signature(spoken):
            errors.append(f"{sid}: spoken and vo fields disagree")
        if row.get("phrases"):
            phrase_spoken = " ".join(compile_shot_text(item["spoken"] if isinstance(item, dict) else item)["spoken"] for item in row["phrases"])
            if spoken_signature(phrase_spoken) != spoken_signature(spoken):
                errors.append(f"{sid}: caption phrases contradict spoken text")
        if bool(spoken) == bool(row.get("silent") or owner != sid):
            errors.append(f"{sid}: spoken content contradicts silent/audio ownership")
        errors.extend(f"{sid}: {err}" for phrase in compiled["phrases"] for err in check_clause_integrity(phrase["spoken"]))
        errors.extend(f"{sid}: {err}" for err in lint_claims(spoken, rules))
        if prompt.is_file():
            paths.append(prompt)
            projected = compile_shot_text(text, default_role=compiled["role"])
            if (projected["role"] != compiled["role"] or
                    spoken_signature(projected["spoken"] if DIALOGUE_RE.search(text) else "") != spoken_signature(spoken)):
                errors.append(f"{sid}: prompt dialogue or role contradicts REBEAT-PLAN")
            errors.extend(lint_prompt(text, shot_id=sid, shot_sec=row.get("audio_source_sec", row["sec"]), is_first_shot=sid == "S01", rules=rules, silent=not spoken)["errors"])
        result["shots"][sid] = compiled
    vo_spoken = compile_remake_text(vo_path.read_text())
    if spoken_signature(vo_spoken) != spoken_signature(" ".join(row["spoken"] for row in result["shots"].values())):
        errors.append("VO-REMAKE.md spoken text contradicts REBEAT-PLAN")
    if errors:
        raise ValueError("; ".join(errors))
    result["inputs"] = {str(path.relative_to(film.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}
    if write:
        lock = film / "qa/SCRIPT-LOCK.json"
        temporary = lock.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(lock)
    return result


def _estimate_english_syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return len(w)  # abbreviations like USB/cm tend to be spelled or concise
    groups = re.findall(r"[aeiouy]+", w)
    cnt = len(groups) or 1
    if w.endswith("e") and cnt > 1:
        cnt -= 1
    return max(1, cnt)


def count_speech_units(text: str) -> float:
    """Count approximate spoken syllable units in Chinese + embedded English/numbers."""
    spoken = project_dual_text(text, side="spoken")
    spoken = re.sub(r"@[A-Za-z0-9_!/{}/-]+", " ", spoken)
    spoken = spoken.replace("||", " ")
    han = len(HAN_RE.findall(spoken))
    en = sum(_estimate_english_syllables(w) for w in LATIN_WORD_RE.findall(spoken))
    digits = sum(max(1, len(d.replace(".", ""))) for d in DIGIT_RUN_RE.findall(spoken))
    return float(han + en + digits)


def estimate_zh_speech(
    text: str,
    *,
    pace: str = "normal",
    rate: float | None = None,
    padding: float = 0.0,
) -> dict[str, Any]:
    """Estimate spoken seconds using Hypit-compatible pronunciation-unit rates."""
    chosen_rate = float(rate) if rate is not None else PACE_RATES_ZH.get(pace, 4.4)
    if chosen_rate <= 0:
        raise ValueError(f"rate must be positive, got {chosen_rate}")
    units = count_speech_units(text)
    raw_sec = (units / chosen_rate) + max(0.0, float(padding))
    return {
        "units": round(units, 2),
        "rate": chosen_rate,
        "pace": pace if rate is None else "custom",
        "padding": round(max(0.0, float(padding)), 3),
        "estimated_sec": round(raw_sec, 2),
        "recommended_sec": max(1, int(math.ceil(raw_sec))),
        "spoken_text": project_dual_text(text, side="spoken").strip(),
    }


def estimate_with_hypit(
    text: str,
    *,
    language: str = "zh",
    pace: str = "normal",
) -> dict[str, Any]:
    """Run `hypit estimate --json` if available, falling back to local estimation."""
    if shutil.which("hypit"):
        try:
            out = subprocess.check_output(
                [
                    "hypit",
                    "estimate",
                    "--text",
                    project_dual_text(text, side="spoken"),
                    "--language",
                    language,
                    "--pace",
                    pace,
                    "--json",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            data = json.loads(out)
            if isinstance(data, dict) and ("seconds" in data or "durationSeconds" in data):
                sec = float(data.get("seconds") or data.get("durationSeconds") or 0.0)
                return {
                    "engine": "hypit",
                    "estimated_sec": round(sec, 2),
                    "recommended_sec": max(1, int(math.ceil(sec))),
                    "raw": data,
                }
        except Exception:
            pass
    local = estimate_zh_speech(text, pace=pace)
    local["engine"] = "local"
    return local


def extract_dialogue_segments(prompt_text: str) -> list[str]:
    """Return spoken strings extracted from `<d>...</d>` tags inside an H3 prompt."""
    out: list[str] = []
    for raw in DIALOGUE_RE.findall(prompt_text):
        cleaned = TAG_PREFIX_RE.sub("", ROLE_RE.sub("", raw)).strip()
        cleaned = project_dual_text(cleaned, side="spoken")
        if cleaned:
            out.append(cleaned)
    return out


def check_clause_integrity(text: str) -> list[str]:
    """Detect mid-thought truncated stubs, dangling connectives, and malformed tags."""
    errs: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        return ["empty spoken text"]

    no_end_punct = stripped.rstrip("，。、！？,.!?… ")
    if not no_end_punct:
        return ["spoken text contains only punctuation"]

    for tail in DANGLING_TAILS:
        if no_end_punct.endswith(tail):
            errs.append(f"dangling truncated clause ending with '{tail}'")
    for word in DANGLING_END_WORDS:
        if no_end_punct.endswith(word) and len(no_end_punct) >= len(word) + 2:
            # Avoid false positives on compound words like '就是' at the start; check boundary.
            if no_end_punct[-len(word) - 1] in "，,。. ":
                errs.append(f"dangling incomplete clause ending with '{word}'")

    open_d = len(re.findall(r"<d>", text, re.IGNORECASE))
    close_d = len(re.findall(r"</d>", text, re.IGNORECASE))
    if open_d != close_d:
        errs.append(f"unbalanced <d>...</d> tags ({open_d} open vs {close_d} close)")
    return errs


def brief_rule_paths(film: Path) -> tuple[Path, ...]:
    return (
        film / "qa" / "BRIEF-RULES.json",
        film.parent / "qa" / "BRIEF-RULES.json",
        film.parent / "_shared" / "BRIEF-RULES.json",
    )


def load_brief_rules(film: Path) -> dict[str, Any]:
    documents = []
    for path in brief_rule_paths(Path(film)):
        if not path.is_file():
            continue
        data = read_strict_json(path)
        for key in ("banned_terms", "banned_patterns"):
            values = data.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"{path.name}: {key} must be a list of strings")
            if key == "banned_patterns":
                for value in values:
                    try:
                        re.compile(value)
                    except re.error as exc:
                        raise ValueError(f"{path.name}: invalid banned pattern {value!r}: {exc}") from exc
        documents.append(data)
    return documents[0] if documents else {}


def lint_claims(text: str, rules: dict[str, Any]) -> list[str]:
    """Check text against `banned_terms` and `banned_patterns` from BRIEF-RULES."""
    errs: list[str] = []
    if not rules:
        return errs
    for term in rules.get("banned_terms") or []:
        t = str(term).strip()
        if t and t in text:
            errs.append(f"forbidden claim/term matched: '{t}'")
    for pat in rules.get("banned_patterns") or []:
        if pat and re.search(str(pat), text):
            errs.append(f"forbidden claim pattern matched: '{pat}'")
    return errs


def lint_prompt(
    prompt_text: str,
    *,
    shot_id: str,
    shot_sec: float | None = None,
    is_first_shot: bool = False,
    rules: dict[str, Any] | None = None,
    silent: bool = False,
    pace: str = "normal",
) -> dict[str, Any]:
    """Run all Hypit-inspired pre-flight checks on one shot's H3 prompt."""
    errors: list[str] = []
    warnings: list[str] = []
    text = (prompt_text or "").strip()
    if not text:
        return {
            "shot": shot_id,
            "ok": False,
            "errors": [f"{shot_id}: empty prompt"],
            "warnings": [],
            "speech": None,
        }

    if not PICTURE_TAG_RE.search(text):
        errors.append(f"{shot_id}: prompt must anchor first frame with <Picture 1>")

    if not is_first_shot and not AUDIO_TAG_RE.search(text):
        warnings.append(
            f"{shot_id}: missing explicit <Audio 1> note (h3_submit will inject default lock)"
        )

    if REQUESTED_SUBTITLE_RE.search(text) and not NEGATED_SUBTITLE_RE.search(text):
        errors.append(
            f"{shot_id}: prompt asks H3 to bake subtitles/price tags; burn captions in 6_assemble instead"
        )

    open_d = len(re.findall(r"<d>", text, re.IGNORECASE))
    close_d = len(re.findall(r"</d>", text, re.IGNORECASE))
    if open_d != close_d:
        errors.append(f"{shot_id}: unbalanced <d>...</d> tags ({open_d} open vs {close_d} close)")

    dialogue = extract_dialogue_segments(text)
    if not dialogue and not silent:
        warnings.append(f"{shot_id}: no <d>...</d> spoken lines found (ok only if intentionally silent)")

    combined_speech = " ".join(dialogue)
    speech_info = estimate_zh_speech(combined_speech, pace=pace) if combined_speech else None
    if combined_speech:
        for seg in dialogue:
            for ce in check_clause_integrity(seg):
                errors.append(f"{shot_id}: {ce} in dialogue '{seg}'")

        if shot_sec is not None and speech_info is not None:
            est = speech_info["estimated_sec"]
            # Allow up to ~1.25x fast margin, flag anything severely overcrowded
            if est > shot_sec * 1.25:
                errors.append(
                    f"{shot_id}: dialogue estimates ~{est:.1f}s ({speech_info['units']:.0f} units "
                    f"at {pace}) > shot duration {shot_sec}s — risk of rushed/clipped speech"
                )
            elif est > shot_sec * 1.05:
                warnings.append(
                    f"{shot_id}: tight timing (~{est:.1f}s vs {shot_sec}s shot); consider 'fast' pace or shorter copy"
                )
            elif est < shot_sec * 0.25 and shot_sec >= 6:
                warnings.append(
                    f"{shot_id}: sparse speech (~{est:.1f}s vs {shot_sec}s shot) — risk of dead air"
                )

    for claim_err in lint_claims(text, rules or {}):
        errors.append(f"{shot_id}: {claim_err}")

    return {
        "shot": shot_id,
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "speech": speech_info,
    }


def lint_film_prompts(
    film: Path,
    *,
    shot_id: str | None = None,
    pace: str = "normal",
) -> dict[str, Any]:
    """Validate `prompts/Sxx.txt` across a film against `REBEAT-PLAN.json` and `BRIEF-RULES.json`."""
    film = Path(film)
    plan_path = film / "qa" / "REBEAT-PLAN.json"
    shots: list[dict[str, Any]] = []
    if plan_path.is_file():
        shots = json.loads(plan_path.read_text()).get("shots") or []
    else:
        shots = [
            {"id": p.stem, "sec": None}
            for p in sorted((film / "prompts").glob("S*.txt"))
        ]

    first_id = shots[0]["id"] if shots else "S01"
    rules = load_brief_rules(film)
    target_shots = [s for s in shots if not shot_id or s["id"] == shot_id]

    results: dict[str, Any] = {}
    all_errors: list[str] = []
    all_warnings: list[str] = []
    for s in target_shots:
        sid = s["id"]
        pfile = film / "prompts" / f"{sid}.txt"
        if not pfile.is_file():
            err = f"{sid}: missing prompt file prompts/{sid}.txt"
            results[sid] = {"shot": sid, "ok": False, "errors": [err], "warnings": [], "speech": None}
            all_errors.append(err)
            continue
        sec = float(s["sec"]) if s.get("sec") is not None else None
        res = lint_prompt(
            pfile.read_text(),
            shot_id=sid,
            shot_sec=sec,
            is_first_shot=(sid == first_id),
            rules=rules,
            silent=bool(s.get("silent")),
            pace=pace,
        )
        results[sid] = res
        all_errors.extend(res["errors"])
        all_warnings.extend(res["warnings"])

    return {
        "ok": len(all_errors) == 0 and len(target_shots) > 0,
        "film": film.name,
        "shots": results,
        "errors": all_errors,
        "warnings": all_warnings,
    }
