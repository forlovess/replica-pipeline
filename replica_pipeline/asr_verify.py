"""Ordered spoken-text verification and hash-bound shot evidence."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

try:
    from .script_lint import DIALOGUE_RE, SHOT_RE, compile_shot_text, read_strict_json
    from .video_qa import file_sha256
except ImportError:
    from script_lint import DIALOGUE_RE, SHOT_RE, compile_shot_text, read_strict_json
    from video_qa import file_sha256


def transcript_text(data: dict) -> str:
    segments = data.get("segments") or []
    if not isinstance(segments, list) or not all(isinstance(row, dict) for row in segments):
        raise ValueError("ASR: expected segments array")
    def words(items):
        return " ".join(item.get("word") or item.get("text") or "" for item in items if isinstance(item, dict))
    return (" ".join(row.get("text") or words(row.get("words") or []) for row in segments).strip()
            or data.get("text") or words(data.get("word_segments") or data.get("words") or []))


DIGITS = dict(zip("零一二三四五六七八九〇两", (*range(10), 0, 2)))
UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
NUMBER = r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?"
TOKEN_RE = re.compile(rf"{NUMBER}|没有|不能|不用|不是|不会|[\u3400-\u9fff]|[a-z]+(?:'[a-z]+)?")


def _number(value: str) -> str:
    if value[0].isdigit():
        return str(Decimal(value).normalize())
    integer, _, fractional = value.partition("点")
    if all(char in DIGITS for char in integer):
        total = int("".join(str(DIGITS[char]) for char in integer))
    else:
        total, section, pending = 0, 0, 0
        for char in integer:
            if char in DIGITS:
                pending = DIGITS[char]
            elif UNITS[char] < 10000:
                section += (pending or 1) * UNITS[char]
                pending = 0
            else:
                if char == "亿":
                    total = (total + section + pending or 1) * UNITS[char]
                else:
                    total += (section + pending or 1) * UNITS[char]
                section, pending = 0, 0
        total += section + pending
    suffix = "." + "".join(str(DIGITS[char]) for char in fractional) if fractional else ""
    return str(Decimal(str(total) + suffix).normalize())


def timed_tokens(text: str) -> list[tuple[str, int, int]]:
    normalized = "".join(unicodedata.normalize("NFKC", char).casefold() for char in text)
    indices = [idx for idx, char in enumerate(text) for _ in unicodedata.normalize("NFKC", char).casefold()]
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", lambda match: "\0" * len(match[0]), normalized)
    kept = [idx for idx, char in enumerate(normalized) if char != "\0"]
    normalized = "".join(normalized[idx] for idx in kept)
    return [("#" + _number(match[0]) if re.fullmatch(NUMBER, match[0]) else match[0],
             indices[kept[match.start()]], indices[kept[match.end() - 1]] + 1) for match in TOKEN_RE.finditer(normalized)]


def _tokens(text: str) -> list[str]:
    return [token for token, _, _ in timed_tokens(text)]


def _distance(expected: list, recognized: list) -> int:
    previous = list(range(len(recognized) + 1))
    for index, token in enumerate(expected, 1):
        current = [index]
        for pos, other in enumerate(recognized, 1):
            current.append(min(previous[pos] + 1, current[-1] + 1, previous[pos - 1] + (token != other)))
        previous = current
    return previous[-1]


def compare_meaning(expected: str, recognized: str, *, min_coverage: float = 0.80, max_cer: float = 0.35) -> dict:
    left, right = _tokens(expected), _tokens(recognized)
    edits = SequenceMatcher(None, left, right, autojunk=False)
    coverage = sum(match.size for match in edits.get_matching_blocks()) / len(left) if left else float(not right)
    cer = _distance(left, right) / max(1, len(left))
    issues = []
    if coverage < min_coverage:
        issues.append("low ordered speech coverage")
    if cer > max_cer:
        issues.append("excessive speech edit distance")
    critical = re.compile(r"#.*|没有|不能|不用|不是|不会|不|没|未|别|无|no|not|never|without|[a-z]+n't")
    if [token for token in left if critical.fullmatch(token)] != [token for token in right if critical.fullmatch(token)]:
        issues.append("number or negation changed")
    removed, inserted = set(), set()
    for operation, start, end, other_start, other_end in edits.get_opcodes():
        if operation == "replace":
            issues.append(f"changed words: {' '.join(left[start:end])} → {' '.join(right[other_start:other_end])}")
        elif operation == "delete" and end - start >= 2:
            issues.append(f"omitted phrase: {' '.join(left[start:end])}")
        if operation == "delete":
            removed.update(left[start:end])
        if operation == "insert":
            inserted.update(right[other_start:other_end])
    if removed & inserted:
        issues.append("spoken words reordered")
    for clause in re.split(r"[，。！？；,.!?;]", expected):
        tokens = _tokens(clause)
        if len(tokens) >= 2 and not any(right[pos:pos + len(tokens)] == tokens for pos in range(len(right))):
            ratio = SequenceMatcher(None, tokens, right, autojunk=False)
            if sum(match.size for match in ratio.get_matching_blocks()) < len(tokens) * min_coverage:
                issues.append(f"missing or altered clause: {clause.strip()}")
    if not left and right:
        issues.append("unexpected speech on silent shot")
    return {"coverage": coverage, "cer": cer, "issues": issues, "verdict": "FAIL" if issues else "PASS"}


def _inputs(film: Path, shot: str) -> tuple[Path, Path, str]:
    if not SHOT_RE.fullmatch(shot):
        raise ValueError(f"invalid shot ID: {shot}")
    prompt = film / "prompts" / f"{shot}.txt"
    raw = prompt.read_text()
    expected = compile_shot_text(raw)["spoken"] if DIALOGUE_RE.search(raw) else ""
    return film / "shots" / f"{shot}.mp4", prompt, expected


def verify_shot_meaning(film: Path, shot: str, *, asr_path=None, language="zh", min_coverage=0.80, max_cer=0.35) -> dict:
    film = Path(film).resolve()
    if not 0 < min_coverage <= 1 or not 0 <= max_cer <= 1:
        raise ValueError("invalid ASR verification thresholds")
    video, prompt, expected = _inputs(film, shot)
    video_sha, prompt_sha = file_sha256(video), file_sha256(prompt)
    lock = film / "qa/H3-MEANING.json"
    data = read_strict_json(lock) if lock.is_file() else {"shots": {}}
    previous = data["shots"].get(shot, {})
    asr = Path(asr_path).resolve() if asr_path is not None else film / "qa/asr" / f"{shot}.json"
    if asr_path is not None and not asr.is_file() and not Path(asr_path).is_absolute():
        asr = film / Path(asr_path)
    stale = asr.is_file() and previous.get("video_sha256", video_sha) != video_sha and previous.get("asr_sha256") == file_sha256(asr)
    if stale and asr_path is not None:
        raise ValueError(f"{shot}: supplied ASR belongs to a previous video")
    if (stale or not asr.is_file()) and asr_path is None:
        if not shutil.which("hypit"):
            raise ValueError(f"{shot}: missing ASR; install hypit or supply --asr-path")
        asr.parent.mkdir(parents=True, exist_ok=True)
        temporary = asr.with_suffix(".transcribing.json")
        try:
            subprocess.run(["hypit", "transcribe", str(video), "--language", language, "--to", str(temporary)], check=True)
            read_strict_json(temporary)
            temporary.replace(asr)
        finally:
            temporary.unlink(missing_ok=True)
    asr_sha = file_sha256(asr)
    recognized = transcript_text(read_strict_json(asr))
    if (file_sha256(video), file_sha256(prompt)) != (video_sha, prompt_sha):
        raise ValueError(f"{shot}: video or prompt changed during ASR verification")
    if file_sha256(asr) != asr_sha:
        raise ValueError(f"{shot}: ASR changed during verification")
    entry = {"video_sha256": video_sha, "prompt_sha256": prompt_sha, "asr_sha256": asr_sha,
             "asr_path": str(asr.relative_to(film)) if asr.is_relative_to(film) else str(asr),
             "expected_spoken": expected, "recognized": recognized, "min_coverage": min_coverage, "max_cer": max_cer,
             **compare_meaning(expected, recognized, min_coverage=min_coverage, max_cer=max_cer)}
    data["shots"][shot] = entry
    tmp = lock.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(lock)
    report = ["# H3 Meaning", ""]
    for sid, row in sorted(data["shots"].items()):
        report.append(f"- {sid} {row['verdict']} — coverage {row['coverage']:.1%}, CER {row['cer']:.1%}"
                      + (": " + "; ".join(row["issues"]) if row["issues"] else ""))
    lock.with_suffix(".md").write_text("\n".join(report) + "\n")
    return entry


def validate_meaning_lock_data(film: Path, shots: list) -> list[str]:
    film = Path(film)
    try:
        rows = read_strict_json(film / "qa/H3-MEANING.json")["shots"]
    except (ValueError, KeyError) as exc:
        return [f"H3-MEANING.json: {exc}"]
    errors = []
    for shot in shots:
        sid = shot if isinstance(shot, str) else shot["id"]
        try:
            row = rows[sid]
            video, prompt, expected = _inputs(film, sid)
            asr = film / row["asr_path"]
            if row["verdict"] != "PASS" or row.get("issues") != []:
                raise ValueError("meaning PASS required")
            if row["expected_spoken"] != expected or any(row.get(key) != file_sha256(path) for key, path in
                    (("video_sha256", video), ("prompt_sha256", prompt), ("asr_sha256", asr))):
                raise ValueError("meaning evidence stale (video, prompt or ASR changed)")
            recognized = transcript_text(read_strict_json(asr))
            comparison = compare_meaning(expected, recognized)
            if row.get("recognized") != recognized or comparison["verdict"] != "PASS" or any(row.get(key) != comparison[key] for key in ("cer", "coverage")):
                raise ValueError("meaning PASS does not match current ASR")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{sid}: {exc}")
    return errors
