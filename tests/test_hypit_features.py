"""Unit tests for Hypit-derived upgrades in replica-pipeline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replica_pipeline import h3_submit, pipeline  # noqa: E402
from replica_pipeline.assemble_tools import (  # noqa: E402
    assemble_film,
    build_ass_subtitles,
    build_ducking_filter,
    format_douyin_caption,
)
from replica_pipeline.candidates import (  # noqa: E402
    archive_current_shot,
    list_candidates,
    select_candidate,
)
from replica_pipeline.hypit_bridge import (  # noqa: E402
    export_hypit_project,
    prepare_source,
)
from replica_pipeline.review_gallery import write_review_html  # noqa: E402
from replica_pipeline.script_lint import (  # noqa: E402
    check_clause_integrity,
    estimate_zh_speech,
    lint_film_prompts,
    lint_prompt,
    project_dual_text,
)


class ScriptLintTests(unittest.TestCase):
    def test_dual_text_and_speech_estimate(self) -> None:
        raw = "只要<¥19.9|十九块九>，<API|A P I>就能直接用。"
        self.assertEqual(project_dual_text(raw, side="display"), "只要¥19.9，API就能直接用。")
        self.assertEqual(project_dual_text(raw, side="spoken"), "只要十九块九，A P I就能直接用。")
        est_norm = estimate_zh_speech("这只粉格双肩包真的很好背", pace="normal")
        est_fast = estimate_zh_speech("这只粉格双肩包真的很好背", pace="fast")
        self.assertEqual(est_norm["units"], 12.0)
        self.assertGreater(est_norm["estimated_sec"], est_fast["estimated_sec"])

    def test_dangling_clause_and_unbalanced_tag_blocked(self) -> None:
        errs = check_clause_integrity("这只包的分区很清楚，再一个就是")
        self.assertTrue(any("再一个就是" in e for e in errs), errs)
        errs2 = check_clause_integrity("<d>没有闭合的台词")
        self.assertTrue(any("unbalanced" in e for e in errs2), errs2)

    def test_lint_prompt_catches_missing_anchor_overrun_and_banned_claim(self) -> None:
        rules = {"banned_terms": ["防水", "99元"]}
        bad = "Presenter talks about the bag. <d>[Chinese] 只要99元而且防水，再一个就是</d>"
        res = lint_prompt(bad, shot_id="S01", shot_sec=4.0, is_first_shot=True, rules=rules)
        self.assertFalse(res["ok"])
        joined = "\n".join(res["errors"])
        self.assertIn("<Picture 1>", joined)
        self.assertIn("99元", joined)
        self.assertIn("防水", joined)
        self.assertIn("再一个就是", joined)

    def test_lint_prompt_passes_clean_shot_and_allows_no_subtitles(self) -> None:
        good = (
            "<Picture 1> shows the mini gingham backpack at shoulder scale. "
            "No subtitles or price tags.\n"
            "<Audio 1> locks the presenter voice.\n"
            "<d>[Chinese] 我先看做工，走线细致。</d>"
        )
        res = lint_prompt(good, shot_id="S02", shot_sec=6.0, is_first_shot=False)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["warnings"], [])

    def test_lint_film_prompts_reads_rebeat_and_brief_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa").mkdir()
            (film / "prompts").mkdir()
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"shots": [{"id": "S01", "sec": 5}, {"id": "S02", "sec": 5}]})
            )
            (film / "qa" / "BRIEF-RULES.json").write_text(json.dumps({"banned_terms": ["特价"]}))
            (film / "prompts" / "S01.txt").write_text(
                "<Picture 1> opening frame. <d>[Chinese] 这只小包轻巧好看。</d>"
            )
            (film / "prompts" / "S02.txt").write_text(
                "<Picture 1> <Audio 1> close look. <d>[Chinese] 特价只要二十。</d>"
            )
            res = lint_film_prompts(film)
            self.assertFalse(res["ok"])
            self.assertTrue(any("S02" in e and "特价" in e for e in res["errors"]), res)


class CandidateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "shots").mkdir()
        (self.film / "tasks").mkdir()
        (self.film / "qa" / "h3-contact-sheets").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archive_clear_and_select_preserves_history(self) -> None:
        vid = self.film / "shots" / "S01.mp4"
        sheet = self.film / "qa" / "h3-contact-sheets" / "S01.jpg"
        task = self.film / "tasks" / "S01.task.json"

        vid.write_bytes(b"render-v1")
        sheet.write_bytes(b"sheet-v1")
        task.write_text(json.dumps({"task_id": "t1", "state": "DOWNLOADED", "duration": 6}))

        c1 = archive_current_shot(self.film, "S01", note="size ok", clear_active=True)
        self.assertIsNotNone(c1)
        assert c1 is not None
        self.assertEqual(c1["candidate_id"], "S01-attempt001")
        self.assertFalse(vid.exists())
        self.assertFalse(task.exists())

        # Second render lands and is archived as attempt002
        vid.write_bytes(b"render-v2-worse")
        sheet.write_bytes(b"sheet-v2")
        task.write_text(json.dumps({"task_id": "t2", "state": "DOWNLOADED", "duration": 6}))
        c2 = archive_current_shot(self.film, "S01", note="too big", clear_active=False)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2["candidate_id"], "S01-attempt002")

        # Switching back to attempt001 restores v1 bytes without losing v2
        sel = select_candidate(self.film, "S01", "S01-attempt001")
        self.assertTrue(sel["ok"])
        self.assertEqual(vid.read_bytes(), b"render-v1")
        self.assertEqual(sheet.read_bytes(), b"sheet-v1")
        listed = list_candidates(self.film, shot="S01")["shots"]["S01"]
        self.assertEqual(listed["active"], "S01-attempt001")
        self.assertEqual(len(listed["items"]), 2)


class AssembleCraftAndReviewTests(unittest.TestCase):
    def test_douyin_caption_formatting_and_ass_generation(self) -> None:
        self.assertEqual(format_douyin_caption("我先看做工，走线细致。"), "我先看做工 走线细致")
        self.assertEqual(format_douyin_caption("<A|эй> 很实用！"), "A 很实用")
        wrapped = format_douyin_caption("这只粉格美乐蒂双肩包真的很好背", max_line_chars=10)
        self.assertIn("\n", wrapped)

        ass = build_ass_subtitles(
            [{"start": 0.2, "end": 1.6, "text": "我先看做工，走线细致。"}],
            width=1152,
            height=2048,
        )
        self.assertIn("PlayResX: 1152", ass)
        self.assertIn(",56,", ass)
        self.assertIn("Dialogue: 0,0:00:00.20,0:00:01.60,Default,,0,0,0,,我先看做工 走线细致", ass)

    def test_ducking_filter_uses_loudnorm_and_sidechain(self) -> None:
        fc = build_ducking_filter(total_dur=18.5, bgm_db=-15.0)
        self.assertIn("loudnorm=I=-16", fc)
        self.assertIn("sidechaincompress", fc)
        self.assertIn("volume=-15.0dB", fc)
        self.assertIn("afade=t=out", fc)

    def test_review_html_renders_single_film_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td)
            film = batch / "01-demo"
            (film / "qa").mkdir(parents=True)
            (film / "qa" / "PIPELINE.json").write_text(json.dumps({"current": "6_assemble", "stages": {}}))
            (film / "qa" / "REBEAT-PLAN.json").write_text(json.dumps({"shots": [{"id": "S01", "sec": 6}]}))
            (film / "qa" / "captions.asr-cues.json").write_text(
                json.dumps([{"start": 0.1, "end": 1.4, "text": "这只小包真好看", "shot": "S01"}])
            )
            (film / "qa" / "captioned.mp4").write_bytes(b"mp4")

            single_html = write_review_html(film=film)
            self.assertTrue(single_html.is_file())
            txt = single_html.read_text()
            self.assertIn("captioned.mp4", txt)
            self.assertIn("这只小包真好看", txt)
            self.assertIn("FEEDBACK.json", txt)

            batch_html = write_review_html(batch=batch)
            self.assertTrue(batch_html.is_file())
            self.assertIn("01-demo/qa/captioned.mp4", batch_html.read_text())


class HypitBridgeTests(unittest.TestCase):
    def test_prepare_source_with_hypit_calls_and_local_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            src = film / "source.mp4"
            src.write_bytes(b"fake-video")

            def fake_run_json(cmd, *, timeout=120):
                if "probe" in cmd:
                    return {"duration": 14.5}
                if "boundaries" in cmd:
                    return {"boundaries": [{"time": 3.2, "score": 0.72}, {"time": 8.9, "score": 0.61}]}
                return {}

            def fake_subproc_run(cmd, **kwargs):
                if "transcribe" in cmd:
                    to_idx = cmd.index("--to") + 1
                    Path(cmd[to_idx]).write_text(
                        json.dumps({"segments": [{"words": [{"word": "好"}, {"word": "看"}]}]})
                    )
                if "tile" in cmd:
                    to_idx = cmd.index("--to") + 1
                    Path(cmd[to_idx]).write_bytes(b"jpg")
                return mock.MagicMock(returncode=0)

            with (
                mock.patch("replica_pipeline.hypit_bridge.has_hypit", return_value=True),
                mock.patch("replica_pipeline.hypit_bridge._run_json", side_effect=fake_run_json),
                mock.patch("replica_pipeline.hypit_bridge.subprocess.run", side_effect=fake_subproc_run),
            ):
                res = prepare_source(film, src, language="zh", use_hypit=True, transcribe=True)

            self.assertEqual(res["duration"], 14.5)
            self.assertEqual(res["cuts_count"], 2)
            self.assertEqual(res["asr_chars"], 2)
            self.assertTrue((film / "qa" / "cuts.json").is_file())
            self.assertTrue((film / "qa" / "source.json").is_file())
            self.assertTrue((film / "qa" / "source-overview.jpg").is_file())

    def test_export_hypit_project_writes_valid_svml_and_svrun(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa").mkdir()
            (film / "prompts").mkdir()
            (film / "shots").mkdir()
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"shots": [{"id": "S01", "sec": 6}, {"id": "S02", "sec": 5}]})
            )
            (film / "prompts" / "S01.txt").write_text("<Picture 1> hook. <d>[Chinese] 姐妹们看过来。</d>")
            (film / "prompts" / "S02.txt").write_text("<Picture 1> <Audio 1> detail. <d>[Chinese] 做工细致。</d>")
            (film / "shots" / "S01.mp4").write_bytes(b"s1")
            (film / "shots" / "S02.mp4").write_bytes(b"s2")
            (film / "qa" / "bgm.wav").write_bytes(b"bgm")

            out = export_hypit_project(film)
            self.assertTrue(out["ok"])
            proj = Path(out["project_dir"])
            pkg = json.loads((proj / "package.json").read_text())
            self.assertTrue(pkg["hypit"]["project"])
            svml = (proj / "production.svml").read_text()
            self.assertIn('@hypit/markup@1', svml)
            self.assertIn('<script id="story">', svml)
            self.assertIn("姐妹们看过来。", svml)
            self.assertIn("bgm-bed", svml)
            self.assertIn('output="final.video"', (proj / "render.svrun").read_text())


class AuditFixRegressionTests(unittest.TestCase):
    def test_assemble_film_fails_closed_when_spoken_asr_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa").mkdir()
            (film / "shots").mkdir()
            (film / "shots" / "S01.mp4").write_bytes(b"mp4")
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"shots": [{"id": "S01", "sec": 5, "spoken": "这只包很好看"}]})
            )
            with mock.patch("replica_pipeline.assemble_tools.shutil.which", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    assemble_film(film, skip_gate=True)

    def test_assemble_silent_and_voiced_shots_with_bgm_real_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa" / "asr").mkdir(parents=True)
            (film / "shots").mkdir()
            # S01: voiced shot (1.2s)
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=pink:s=360x640:d=1.2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(film / "shots" / "S01.mp4"),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            # S02: video-only silent B-roll (1.0s, NO audio stream)
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1.0",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                    str(film / "shots" / "S02.mp4"),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            # BGM wav
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3.0", str(film / "qa" / "bgm.wav")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"shots": [{"id": "S01", "sec": 4, "spoken": "好包"}, {"id": "S02", "sec": 4}]})
            )
            (film / "qa" / "asr" / "S01.json").write_text(
                json.dumps({"segments": [{"words": [{"word": "好", "start": 0.2, "end": 0.5}, {"word": "包", "start": 0.5, "end": 0.9}]}]})
            )

            def fake_burn(video_path, cues, out_path, **kwargs):
                Path(out_path).write_bytes(Path(video_path).read_bytes())
                return Path(out_path)

            with mock.patch("replica_pipeline.assemble_tools.burn_subtitles", side_effect=fake_burn):
                res = assemble_film(film, bgm_path=film / "qa" / "bgm.wav", bgm_source="source", width=576, height=1024, skip_gate=True)
            self.assertTrue(res["ok"])
            self.assertTrue(res["assemble_check"]["bgm_ducked"])
            self.assertEqual(res["assemble_check"]["cues_count"], 1)
            self.assertTrue((film / "qa" / "captioned.mp4").is_file())

    def test_require_blocks_stale_voice_lock_and_tampered_shots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa" / "voice-lock").mkdir(parents=True)
            (film / "shots").mkdir()
            (film / "first-frames" / "h3").mkdir(parents=True)
            (film / "first-frames" / "h3" / "S01-first.jpg").write_bytes(b"s1")
            (film / "first-frames" / "h3" / "S02-first.jpg").write_bytes(b"s2")
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"source_basis": "scene_merge", "shots": [{"id": "S01", "sec": 5}, {"id": "S02", "sec": 5}]})
            )
            (film / "shots" / "S01.mp4").write_bytes(b"new-rerolled-s01")
            (film / "qa" / "voice-lock" / "S01-voice.wav").write_bytes(b"wav")
            (film / "qa" / "VOICE-LOCK.json").write_text(json.dumps({"source_sha256": "deadbeef-old-s01"}))

            state = pipeline.blank_state("demo")
            for s in ("1_read_source", "2_vo_adapt", "3_beat_plan", "4_stills"):
                state["stages"][s]["status"] = "pass"
            pipeline.refresh_locks(state)
            pipeline.save(film, state)

            # Even if still locks weren't valid here, test stale voice check via mocking still validations cleanly
            with (
                mock.patch("replica_pipeline.pipeline.validate_size_lock", return_value=[]),
                mock.patch("replica_pipeline.pipeline.validate_person_lock", return_value=[]),
                mock.patch("replica_pipeline.still_qa.validate_still_sheet", return_value=[]),
                mock.patch("replica_pipeline.still_qa.validate_product_lock_data", return_value=[]),
                mock.patch("replica_pipeline.vlm_review.validate_vlm_still_review_data", return_value=[]),
            ):
                (film / "qa" / "SIZE-LOCK.json").write_text("{}")
                (film / "qa" / "PERSON-LOCK.json").write_text("{}")
                (film / "qa" / "STILL-PRODUCT-LOCK.json").write_text("{}")
                (film / "qa" / "VLM-STILL-REVIEW.json").write_text("{}")
                rc = pipeline.cmd_require(argparse.Namespace(film=str(film), action="h3_submit", shot="S02"))
            self.assertEqual(rc, 2)

            # Tampering with 5_h3 blocks require assemble even if PIPELINE.json says pass
            state["stages"]["5_h3"]["status"] = "pass"
            pipeline.refresh_locks(state)
            pipeline.save(film, state)
            rc_asm = pipeline.cmd_require(argparse.Namespace(film=str(film), action="assemble", shot=None))
            self.assertEqual(rc_asm, 2)

    def test_h3_submit_env_overrides_and_review_html_xss_escape(self) -> None:
        with mock.patch.dict(os.environ, {"H3_BASE_URL": "http://127.0.0.1:9999/", "H3_API_KEY": "secret-env"}):
            self.assertEqual(h3_submit.base_url(), "http://127.0.0.1:9999")
            self.assertEqual(h3_submit.api_key(), "secret-env")

        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa").mkdir()
            (film / "qa" / "FEEDBACK.json").write_text(
                json.dumps({"comments": [{"id": "c1", "at": 1.0, "text": "<img src=x onerror=alert(1)>"}]})
            )
            html_p = write_review_html(film=film)
            content = html_p.read_text()
            self.assertIn("function esc(s)", content)
            self.assertIn("${esc(c.text)}", content)


if __name__ == "__main__":
    unittest.main()
