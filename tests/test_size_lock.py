"""Smoke tests for structured user_spec size evidence."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from replica_pipeline.size_lock import (  # noqa: E402
    apply_size_review,
    build_size_lock,
    validate_size_lock_data,
)


class SizeLockUserSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.film = Path(self.tmp.name)
        (self.film / "qa").mkdir()
        # canon still on disk
        still = self.film / "first-frames" / "h3"
        still.mkdir(parents=True)
        (still / "S01-first.jpg").write_bytes(b"x")
        self.shots = [{"id": "S01", "sec": 10}, {"id": "S02", "sec": 8}]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bare_pass_string_fails_under_user_spec(self) -> None:
        data = build_size_lock(
            source="user_spec",
            canon_still="first-frames/h3/S01-first.jpg",
            shots={"S01": "PASS", "S02": "PASS"},
            physical_cm={"w": 26, "h": 27, "d": 13},
            size_diagram="_shared/front-with-size.jpg",
        )
        # diagram missing on disk + forbid flag set by build — create dummy diagram path miss
        # build sets forbid true; diagram path will error — strip diagram for this case
        data.pop("size_diagram", None)
        errs = validate_size_lock_data(data, self.film, self.shots)
        joined = "\n".join(errs)
        self.assertIn("must be an object", joined)

    def test_same_source_occupancy_fails(self) -> None:
        data = build_size_lock(
            source="user_spec",
            canon_still="first-frames/h3/S01-first.jpg",
            shots={
                "S01": {
                    "status": "PASS",
                    "mode": "worn",
                    "vs_user_spec": "match",
                    "vs_source_occupancy": "same",
                    "refs_used": ["front-with-size.jpg", "worn-scale-ref.jpg"],
                },
                "S02": {
                    "status": "PASS",
                    "mode": "handheld",
                    "vs_user_spec": "match",
                    "vs_source_occupancy": "smaller",
                    "refs_used": ["front-with-size.jpg"],
                },
            },
            physical_cm={"w": 26, "h": 27, "d": 13},
            worn_scale_ref="_shared/worn-scale-ref.jpg",
        )
        errs = validate_size_lock_data(data, self.film, self.shots)
        self.assertTrue(any("vs_source_occupancy" in e and "S01" in e for e in errs))

    def test_structured_pass(self) -> None:
        data = build_size_lock(
            source="user_spec",
            canon_still="first-frames/h3/S01-first.jpg",
            shots={},
            physical_cm={"w": 26, "h": 27, "d": 13},
            worn_scale_ref="_shared/worn-scale-ref.jpg",
            phrase="迷你约26×27×13cm",
        )
        data = apply_size_review(
            data,
            "S01",
            mode="worn",
            vs_source_occupancy="smaller",
            refs_used=["front-with-size.jpg", "worn-scale-ref.jpg"],
        )
        data = apply_size_review(
            data,
            "S02",
            mode="handheld",
            vs_source_occupancy="n/a",
            refs_used=["front-with-size.jpg"],
        )
        errs = validate_size_lock_data(data, self.film, self.shots)
        self.assertEqual(errs, [], errs)

    def test_first_frame_still_allows_string_pass(self) -> None:
        data = build_size_lock(
            source="first_frame",
            canon_still="first-frames/h3/S01-first.jpg",
            shots={"S01": "PASS", "S02": "PASS"},
            phrase="match S01",
        )
        errs = validate_size_lock_data(data, self.film, self.shots)
        self.assertEqual(errs, [], errs)


if __name__ == "__main__":
    unittest.main()
