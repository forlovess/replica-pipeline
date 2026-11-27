"""Unit tests for pre-H3 still geometry, comparison board, structured locks, and VLM review."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replica_pipeline.person_lock import (  # noqa: E402
    build_person_lock,
    validate_person_lock_data,
)
from replica_pipeline.pipeline import validate_stills  # noqa: E402
from replica_pipeline.still_qa import (  # noqa: E402
    SHEET_MANIFEST_NAME,
    apply_product_review,
    build_product_lock,
    check_still_geometry,
    inspect_film_stills,
    render_label_ppm,
    validate_product_lock_data,
    validate_still_sheet,
)
from replica_pipeline.vlm_review import (  # noqa: E402
    review_still_with_vlm,
    review_video_with_vlm,
    validate_vlm_still_review_data,
)


def _make_jpg(path: Path, w: int = 576, h: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=pink:s={w}x{h}:d=1", "-frames:v", "1", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


class StillGeometryAndProductLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "qa").mkdir()
        (self.film / "qa" / "REBEAT-PLAN.json").write_text(
            json.dumps({"source_basis": "scene_merge", "shots": [{"id": "S01", "sec": 8}, {"id": "S02", "sec": 8}]})
        )
        _make_jpg(self.film / "first-frames" / "h3" / "S01-first.jpg", 576, 1024)
        _make_jpg(self.film / "first-frames" / "h3" / "S02-first.jpg", 576, 1024)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_geometry_rejects_landscape_and_accepts_portrait(self) -> None:
        ok, info, errs = check_still_geometry(self.film / "first-frames" / "h3" / "S01-first.jpg")
        self.assertTrue(ok, errs)
        self.assertEqual((info["width"], info["height"]), (576, 1024))

        bad = self.film / "bad.jpg"
        _make_jpg(bad, 1024, 576)
        ok_bad, _, errs_bad = check_still_geometry(bad)
        self.assertFalse(ok_bad)
        self.assertTrue(any("portrait" in e for e in errs_bad))

    def test_inspect_stills_builds_comparison_sheet(self) -> None:
        res = inspect_film_stills(self.film)
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.film / "qa" / "still-contact-sheet.jpg").is_file())
        manifest = json.loads((self.film / SHEET_MANIFEST_NAME).read_text())
        self.assertEqual(manifest["stills"].keys(), {"S01", "S02"})
        self.assertTrue(manifest["sheet_sha256"])

    def test_inspect_stills_without_stills_does_not_crash(self) -> None:
        empty = self.film / "empty_film"
        (empty / "qa").mkdir(parents=True)
        (empty / "qa" / "REBEAT-PLAN.json").write_text(
            json.dumps({"shots": [{"id": "S01", "sec": 8}]})
        )
        res = inspect_film_stills(empty)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["comparison_sheet"])
        self.assertIn("no first-frames", res["error"])

    def test_board_manifest_blocks_redrawn_still(self) -> None:
        shots = [{"id": "S01"}, {"id": "S02"}]
        inspect_film_stills(self.film)
        self.assertEqual(validate_still_sheet(self.film, shots), [])

        # Regenerate a still the board was built from -> board is stale
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=576x1024:d=1",
             "-frames:v", "1", str(self.film / "first-frames" / "h3" / "S02-first.jpg")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        errs = validate_still_sheet(self.film, shots)
        self.assertTrue(any("S02 stale" in e for e in errs), errs)

    def test_label_renderer_emits_valid_ppm(self) -> None:
        blob = render_label_ppm("SIZE-DIAGRAM", 270, 28)
        header = b"P6\n270 28\n255\n"
        self.assertTrue(blob.startswith(header))
        self.assertEqual(len(blob), len(header) + 270 * 28 * 3)

    def test_product_lock_bans_bare_pass_and_checks_sha(self) -> None:
        shots = [{"id": "S01"}, {"id": "S02"}]
        bare = build_product_lock({"S01": "PASS", "S02": "PASS"})
        errs = validate_product_lock_data(bare, self.film, shots)
        self.assertEqual(len(errs), 2)
        self.assertIn("bare PASS forbidden", errs[0])

        doc = build_product_lock()
        doc = apply_product_review(self.film, doc, "S01")
        doc = apply_product_review(self.film, doc, "S02")
        self.assertEqual(validate_product_lock_data(doc, self.film, shots), [])

        # Redraw S01 invalidates its product lock
        _make_jpg(self.film / "first-frames" / "h3" / "S01-first.jpg", 576, 1024)
        # Re-encode with slightly different color to guarantee sha change
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=white:s=576x1024:d=1", "-frames:v", "1",
             str(self.film / "first-frames" / "h3" / "S01-first.jpg")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        errs2 = validate_product_lock_data(doc, self.film, shots)
        self.assertTrue(any("still_sha256 mismatch" in e for e in errs2), errs2)

    def test_person_lock_bans_bare_pass(self) -> None:
        (self.film / "qa" / "presenter-place.md").write_text("mom")
        pdata = build_person_lock(
            canon_still="first-frames/h3/S01-first.jpg",
            traits={"gender": "女", "apparent_age": "成人", "hair": "长发", "outfit": "白Polo"},
            shots={"S01": "PASS"},
        )
        errs = validate_person_lock_data(pdata, self.film, [{"id": "S01"}])
        self.assertTrue(any("bare PASS forbidden" in e for e in errs), errs)


class VlmReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "qa").mkdir()
        (self.film / "shots").mkdir()
        (self.film / "qa" / "REBEAT-PLAN.json").write_text(
            json.dumps({"source_basis": "scene_merge", "shots": [{"id": "S01", "sec": 8}]})
        )
        _make_jpg(self.film / "first-frames" / "h3" / "S01-first.jpg", 576, 1024)
        (self.film / "shots" / "S01.mp4").write_bytes(b"mp4-v1")
        _make_jpg(self.film / "qa" / "h3-contact-sheets" / "S01.jpg", 1080, 480)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_vlm_still_review_pass_fail_and_forged_reviewer(self) -> None:
        fake_fail = (
            {
                "verdict": "FAIL",
                "size_match": False,
                "identity_match": True,
                "person_match": True,
                "no_extra_tags": True,
                "issues": ["Bag is 2x head height (oversized)"],
            },
            "codex-cli",
        )
        with mock.patch("replica_pipeline.vlm_review.invoke_vlm", return_value=fake_fail):
            entry = review_still_with_vlm(self.film, "S01")
        self.assertEqual(entry["verdict"], "FAIL")

        doc = json.loads((self.film / "qa" / "VLM-STILL-REVIEW.json").read_text())
        errs = validate_vlm_still_review_data(doc, self.film, [{"id": "S01"}])
        self.assertTrue(any("verdict!=PASS" in e for e in errs), errs)

        # Forged non-vlm reviewer is rejected
        doc["shots"]["S01"].update(verdict="PASS", size_match=True, reviewer="self-signed")
        errs_forged = validate_vlm_still_review_data(doc, self.film, [{"id": "S01"}])
        self.assertTrue(any("must start with 'vlm:'" in e for e in errs_forged), errs_forged)

        # Genuine VLM PASS passes, and redrawing still invalidates it
        fake_pass = (
            {
                "verdict": "PASS",
                "size_match": True,
                "identity_match": True,
                "person_match": True,
                "no_extra_tags": True,
                "issues": [],
            },
            "codex-cli",
        )
        with mock.patch("replica_pipeline.vlm_review.invoke_vlm", return_value=fake_pass) as m_vlm:
            entry2 = review_still_with_vlm(self.film, "S01")
        self.assertEqual(entry2["verdict"], "PASS")
        prompt_sent = m_vlm.call_args.args[1]
        self.assertNotIn("2/3 of back width", prompt_sent)
        doc2 = json.loads((self.film / "qa" / "VLM-STILL-REVIEW.json").read_text())
        self.assertEqual(validate_vlm_still_review_data(doc2, self.film, [{"id": "S01"}]), [])

        # Changing a referenced scale image invalidates cached VLM pass via refs_sha256
        ref = self.film / "qa" / "worn-scale-ref.jpg"
        _make_jpg(ref, 576, 1024)
        (self.film / "qa" / "SIZE-LOCK.json").write_text(
            json.dumps({"source": "user_spec", "worn_scale_ref": "qa/worn-scale-ref.jpg"})
        )
        with mock.patch("replica_pipeline.vlm_review.invoke_vlm", return_value=fake_pass):
            review_still_with_vlm(self.film, "S01")
        doc3 = json.loads((self.film / "qa" / "VLM-STILL-REVIEW.json").read_text())
        self.assertEqual(validate_vlm_still_review_data(doc3, self.film, [{"id": "S01"}]), [])
        ref.write_bytes(b"changed-scale-diagram")
        errs_ref = validate_vlm_still_review_data(doc3, self.film, [{"id": "S01"}])
        self.assertTrue(any("worn_scale_ref" in e and "sha256 mismatch" in e for e in errs_ref), errs_ref)

        # Validate that pipeline.validate_stills blocks when any of the 4 locks is missing
        errs_stills = validate_stills(self.film)
        self.assertTrue(any("still-contact-sheet.jpg" in e for e in errs_stills), errs_stills)

    def test_vlm_video_review_writes_visual_lock(self) -> None:
        fake_vpass = (
            {
                "verdict": "PASS",
                "size_stable": True,
                "size_vs_spec": "match",
                "identity_intact": True,
                "no_baked_captions": True,
                "no_hallucinated_tags": True,
                "person_consistent": True,
                "issues": [],
            },
            "codex-cli",
        )
        with mock.patch("replica_pipeline.vlm_review.invoke_vlm", return_value=fake_vpass):
            entry = review_video_with_vlm(self.film, "S01")
        self.assertEqual(entry["status"], "PASS")
        self.assertEqual(entry["reviewer"], "vlm:codex-cli")
        self.assertTrue((self.film / "qa" / "H3-VISUAL-LOCK.json").is_file())


if __name__ == "__main__":
    unittest.main()
