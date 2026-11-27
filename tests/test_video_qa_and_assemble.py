"""Unit tests for post-H3 visual QA, hash locks, and ASR-aligned assemble validation."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replica_pipeline.assemble_tools import (  # noqa: E402
    align_phrases_to_asr,
    min_font_size,
    recommended_font_size,
    validate_assemble_data,
)
from replica_pipeline.person_lock import (  # noqa: E402
    apply_person_review,
    build_person_lock,
    validate_person_lock_data,
)
from replica_pipeline.size_lock import (  # noqa: E402
    apply_size_review,
    build_size_lock,
    validate_size_lock_data,
)
from replica_pipeline.video_qa import (  # noqa: E402
    apply_video_review,
    build_visual_lock,
    find_active_segments,
    validate_visual_lock_data,
)


class VideoVisualLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "shots").mkdir()
        (self.film / "qa" / "h3-contact-sheets").mkdir(parents=True)
        (self.film / "shots" / "S01.mp4").write_bytes(b"video-v1")
        (self.film / "qa" / "h3-contact-sheets" / "S01.jpg").write_bytes(b"sheet")
        self.shots = [{"id": "S01", "sec": 8}]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_visual_lock_pass_and_sha_invalidation(self) -> None:
        data = build_visual_lock()
        data = apply_video_review(
            self.film,
            data,
            "S01",
            size_stable=True,
            size_vs_spec="match",
            identity_intact=True,
            no_baked_captions=True,
            no_hallucinated_tags=True,
            person_consistent=True,
        )
        self.assertEqual(validate_visual_lock_data(data, self.film, self.shots), [])

        # Re-rendering S01.mp4 invalidates prior PASS automatically
        (self.film / "shots" / "S01.mp4").write_bytes(b"video-v2-rerendered")
        errs = validate_visual_lock_data(data, self.film, self.shots)
        self.assertTrue(any("shot_sha256 mismatch" in e for e in errs), errs)

    def test_visual_review_blocks_pass_on_bad_flag(self) -> None:
        data = build_visual_lock()
        with self.assertRaises(ValueError):
            apply_video_review(
                self.film,
                data,
                "S01",
                size_stable=False,
                size_vs_spec="match",
            )


class StillHashInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "qa").mkdir()
        (self.film / "qa" / "presenter-place.md").write_text("mom")
        (self.film / "first-frames" / "h3").mkdir(parents=True)
        (self.film / "first-frames" / "h3" / "S01-first.jpg").write_bytes(b"still-v1")
        self.shots = [{"id": "S01", "sec": 8}]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_size_and_person_still_sha_invalidate_on_redraw(self) -> None:
        sdata = build_size_lock(
            source="user_spec",
            canon_still="first-frames/h3/S01-first.jpg",
            shots={},
            physical_cm={"w": 26, "h": 27, "d": 13},
        )
        sdata = apply_size_review(
            sdata,
            "S01",
            mode="handheld",
            vs_source_occupancy="n/a",
            refs_used=["front-with-size.jpg"],
            film=self.film,
        )
        pdata = build_person_lock(
            canon_still="first-frames/h3/S01-first.jpg",
            traits={"gender": "女", "apparent_age": "成人", "hair": "长发", "outfit": "白衣"},
            shots={},
        )
        pdata = apply_person_review(pdata, "S01", film=self.film)

        self.assertEqual(validate_size_lock_data(sdata, self.film, self.shots), [])
        self.assertEqual(validate_person_lock_data(pdata, self.film, self.shots), [])

        # Redrawing S01-first.jpg invalidates both locks
        (self.film / "first-frames" / "h3" / "S01-first.jpg").write_bytes(b"still-v2")
        self.assertTrue(any("still_sha256" in e for e in validate_size_lock_data(sdata, self.film, self.shots)))
        self.assertTrue(any("still_sha256" in e for e in validate_person_lock_data(pdata, self.film, self.shots)))


class AssembleToolsTests(unittest.TestCase):
    def test_active_segments_detects_leading_glitch(self) -> None:
        # 0.05-0.20s isolated burst (-15dB), then 0.20-0.90s silence (-60dB), then main speech
        dbs = [-60.0] * 40
        for i in range(1, 4):  # 0.05..0.20
            dbs[i] = -15.0
        for i in range(18, 35):  # 0.90..1.75
            dbs[i] = -12.0
        segs = find_active_segments(dbs)
        self.assertEqual(segs, [(0.05, 0.2), (0.9, 1.75)])

    def test_align_phrases_clips_whisperx_pause_stretch(self) -> None:
        # "我先看做工" (0.50-1.30), then 2.5s silence, then "走线细致" (3.80-4.60)
        # WhisperX stretches "工".end all the way to 3.80s.
        chars = [
            {"text": "下", "start": 0.05, "end": 0.20},  # skipped glitch char
            {"text": "我", "start": 0.50, "end": 0.70},
            {"text": "先", "start": 0.70, "end": 0.90},
            {"text": "看", "start": 0.90, "end": 1.05},
            {"text": "做", "start": 1.05, "end": 1.20},
            {"text": "工", "start": 1.20, "end": 3.80},
            {"text": "走", "start": 3.80, "end": 4.00},
            {"text": "线", "start": 4.00, "end": 4.20},
            {"text": "细", "start": 4.20, "end": 4.40},
            {"text": "致", "start": 4.40, "end": 4.60},
        ]
        dbs = [-60.0] * 100
        for i in range(10, 27):  # 0.50..1.35 loud
            dbs[i] = -12.0
        for i in range(76, 93):  # 3.80..4.65 loud
            dbs[i] = -12.0

        with mock.patch("replica_pipeline.assemble_tools.audio_db_bins", return_value=dbs):
            cues = align_phrases_to_asr(
                Path("dummy.mp4"),
                chars,
                ["我先看做工", "走线细致"],
                trim_t0=0.42,
                clip_dur=4.4,
                shot="S03",
                skip_head=1,
            )
        self.assertEqual(len(cues), 2)
        # First cue should end near 1.35 - 0.42 + 0.10 = 1.03s, NOT stretched to 3.38s!
        self.assertLess(cues[0]["end"], 1.30)
        self.assertGreater(cues[1]["start"], 3.30)

    def test_validate_assemble_enforces_asr_align_and_font_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            film = Path(tmp)
            (film / "qa").mkdir()
            (film / "qa" / "cues.json").write_text("[]")
            # Tiny 34px + linear align should fail
            (film / "qa" / "assemble-check.json").write_text(
                json.dumps(
                    {
                        "bgm_source": "new",
                        "caption_align": "linear",
                        "width": 1152,
                        "caption_font_px": 34,
                        "leading_glitch_trimmed": False,
                        "caption_cues_path": "qa/cues.json",
                    }
                )
            )
            errs = validate_assemble_data(film)
            self.assertEqual(len(errs), 3, errs)

            # Valid config passes
            (film / "qa" / "assemble-check.json").write_text(
                json.dumps(
                    {
                        "bgm_source": "source",
                        "caption_align": "asr_words",
                        "width": 1152,
                        "caption_font_px": recommended_font_size(1152),
                        "leading_glitch_trimmed": True,
                        "caption_cues_path": "qa/cues.json",
                    }
                )
            )
            self.assertEqual(recommended_font_size(1152), 56)
            self.assertGreaterEqual(recommended_font_size(1152), min_font_size(1152))
            self.assertEqual(validate_assemble_data(film), [])


if __name__ == "__main__":
    unittest.main()
