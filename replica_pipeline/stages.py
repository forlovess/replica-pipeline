"""Replica pipeline stages — fail-closed gates from /replica skill."""
from __future__ import annotations

STAGE_ORDER = [
    "1_read_source",
    "2_vo_adapt",
    "3_beat_plan",
    "4_stills",
    "5_h3",
    "6_assemble",
    "7_superres",
]

STAGE_TITLE = {
    "1_read_source": "读源片（时长/切镜/人物场所/ASR）",
    "2_vo_adapt": "口播 extract-then-adapt（密度锁）",
    "3_beat_plan": "拍表（源切点/意群 → 4–15s）",
    "4_stills": "静帧（源首帧换品：身份+比例+人物）",
    "5_h3": "H3 生成 + 听感",
    "6_assemble": "字幕 + BGM（优先源配乐）",
    "7_superres": "超分（须用户确认）",
}

# Artifacts required before a stage can be marked pass.
# Paths are relative to the film directory.
REQUIRED = {
    "1_read_source": [
        "qa/source.json",       # {path, duration, cuts_count, cuts_path, asr_chars, asr_path}
        "qa/cuts.json",         # cut times + merge plan basis
        "qa/presenter-place.md",
    ],
    "2_vo_adapt": [
        "qa/VO-REMAKE.md",      # full remake script + per-shot lines
        "qa/vo-density.json",   # {source_chars, remake_chars, ratio, facts_exhausted: bool}
    ],
    "3_beat_plan": [
        "qa/REBEAT-PLAN.json",  # shots[] with sec in [4,15], source basis
    ],
    "4_stills": [
        "qa/STILL-PRODUCT-LOCK.md",
        "qa/SIZE-LOCK.json",  # film-level product scale (user_spec | first_frame)
        "qa/PERSON-LOCK.json",  # same presenter across shots (source-level continuity)
    ],
    "5_h3": [
        # validated dynamically: every beat shot has shots/Sxx.mp4 + meaning pass note
        # + VOICE-LOCK.json after S01 (S02+ must reuse that wav via ref2v)
    ],
    "6_assemble": [
        "qa/captioned.mp4",
        "qa/strict-with-bgm.mp4",
        "qa/assemble-check.json",
    ],
    "7_superres": [
        "qa/superres-allow.json",  # {"allow": true, "by": "user"}
    ],
}

# Which stage must be PASS before an action is allowed.
ACTION_REQUIRES = {
    "h3_submit": "4_stills",
    "assemble": "5_h3",
    "superres": "6_assemble",
}
