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
import math
import re
import shutil
import subprocess
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
        cleaned = TAG_PREFIX_RE.sub("", raw).strip()
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


def load_brief_rules(film: Path) -> dict[str, Any]:
    """Load forbidden claims / prompt rules from `qa/BRIEF-RULES.json` or batch parent."""
    film = Path(film)
    for cand in (
        film / "qa" / "BRIEF-RULES.json",
        film.parent / "qa" / "BRIEF-RULES.json",
        film.parent / "_shared" / "BRIEF-RULES.json",
    ):
        if cand.is_file():
            try:
                data = json.loads(cand.read_text())
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


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
