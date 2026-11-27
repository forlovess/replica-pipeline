"""Unit tests for source vocals/BGM separation (`audio_stem.py`) and end-to-end BGM reuse."""
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

from replica_pipeline.assemble_tools import assemble_film  # noqa: E402
from replica_pipeline.audio_stem import extract_source_stems  # noqa: E402
from replica_pipeline.hypit_bridge import prepare_source  # noqa: E402


def _make_wav_from_expr(path: Path, expr: str, *, channels: int = 2, dur: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={dur}:sample_rate=44100",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={dur}:sample_rate=44100",
            "-filter_complex",
            expr,
            "-ac",
            str(channels),
            str(path),
        ],
        check=True,
    )


class AudioStemTests(unittest.TestCase):
    def test_dry_center_speech_marks_no_usable_bgm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            src = film / "src_dry.mp4"
            # Identical L and R sine wave -> L-R residual is silence -> no BGM
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=360x640:d=1.5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=350:duration=1.5",
                    "-filter_complex",
                    "[1:a]pan=stereo|c0=c0|c1=c0[aout]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(src),
                ],
                check=True,
            )
            res = extract_source_stems(film, src, engine="center-cancel")
            self.assertTrue(res["has_audio"])
            self.assertFalse(res["usable_bgm"])
            self.assertTrue((film / "qa" / "source-vocals.wav").is_file())
            self.assertFalse((film / "qa" / "bgm.wav").exists())

    def test_stereo_bgm_extracted_cached_and_reused_by_assemble(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            (film / "qa" / "asr").mkdir(parents=True)
            (film / "shots").mkdir()
            src = film / "src_with_bgm.mp4"

            # Center speech (440Hz L=R) + genuine stereo side music (220Hz only in L, silent in R)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=360x640:d=1.6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1.6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:duration=1.6",
                    "-filter_complex",
                    "[1:a]asplit=2[vL][vR];"
                    "[vL][2:a]amix=inputs=2:duration=first[L];"
                    "[L][vR]amerge=inputs=2[aout]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(src),
                ],
                check=True,
            )

            res = extract_source_stems(film, src, engine="center-cancel")
            self.assertTrue(res["usable_bgm"], res)
            self.assertTrue((film / "qa" / "bgm.wav").is_file())
            self.assertTrue((film / "qa" / "source-vocals.wav").is_file())

            # Idempotent cache hit without invoking separation again
            with mock.patch("replica_pipeline.audio_stem._separate_with_center_cancel") as m_sep:
                cached = extract_source_stems(film, src, engine="center-cancel")
            m_sep.assert_not_called()
            self.assertEqual(cached["bgm_sha256"], res["bgm_sha256"])

            # Assemble automatically picks up qa/bgm.wav (bgm_source='source') with silent H3 B-roll
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=pink:s=360x640:d=1.2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(film / "shots" / "S01.mp4"),
                ],
                check=True,
            )
            (film / "qa" / "REBEAT-PLAN.json").write_text(
                json.dumps({"shots": [{"id": "S01", "sec": 4}]})
            )
            asm = assemble_film(film, width=576, height=1024, skip_gate=True, allow_uncaptioned=True)
            self.assertTrue(asm["ok"])
            self.assertEqual(asm["assemble_check"]["bgm_source"], "source")
            self.assertTrue(asm["assemble_check"]["bgm_ducked"])

    def test_no_audio_and_mono_center_cancel_handled_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            no_audio = film / "silent.mp4"
            mono = film / "mono.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x320:d=0.6",
                    "-c:v",
                    "libx264",
                    "-an",
                    str(no_audio),
                ],
                check=True,
            )
            res = extract_source_stems(film, no_audio, engine="auto")
            self.assertFalse(res["has_audio"])
            self.assertFalse(res["usable_bgm"])

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x320:d=0.6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.6",
                    "-ac",
                    "1",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(mono),
                ],
                check=True,
            )
            with self.assertRaises(RuntimeError):
                extract_source_stems(film, mono, engine="center-cancel", force=True)

    def test_demucs_flow_and_prepare_source_transcribes_vocals_stem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            film = Path(td)
            src = film / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=360x640:d=1.2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1.2",
                    "-ac",
                    "2",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(src),
                ],
                check=True,
            )
            fake_voc = film / "fake_voc.wav"
            fake_bgm = film / "fake_bgm.wav"
            _make_wav_from_expr(fake_voc, "[0:a]volume=0.8", dur=1.2)
            _make_wav_from_expr(fake_bgm, "[1:a]volume=0.6", dur=1.2)

            transcribe_inputs: list[str] = []
            real_run = subprocess.run

            def fake_subproc_run(cmd, **kwargs):
                if cmd and cmd[0] == "hypit":
                    if "transcribe" in cmd:
                        transcribe_inputs.append(cmd[2])
                        to_idx = cmd.index("--to") + 1
                        Path(cmd[to_idx]).write_text(json.dumps({"segments": [{"words": [{"word": "好"}]}]}))
                    elif "tile" in cmd:
                        to_idx = cmd.index("--to") + 1
                        Path(cmd[to_idx]).write_bytes(b"jpg")
                    return mock.MagicMock(returncode=0)
                return real_run(cmd, **kwargs)

            with (
                mock.patch(
                    "replica_pipeline.audio_stem.resolve_stem_engine",
                    return_value=("demucs", ["demucs"]),
                ),
                mock.patch(
                    "replica_pipeline.audio_stem._separate_with_demucs",
                    return_value=(fake_voc, fake_bgm),
                ),
                mock.patch("replica_pipeline.hypit_bridge.has_hypit", return_value=True),
                mock.patch("replica_pipeline.hypit_bridge._run_json", return_value={"duration": 1.2, "boundaries": []}),
                mock.patch("replica_pipeline.hypit_bridge.subprocess.run", side_effect=fake_subproc_run),
            ):
                prep = prepare_source(
                    film,
                    src,
                    language="zh",
                    use_hypit=True,
                    transcribe=True,
                    extract_bgm=True,
                )

            self.assertTrue(prep["stem_split"]["usable_bgm"])
            self.assertEqual(prep["stem_split"]["bgm"], "qa/bgm.wav")
            # WhisperX transcription should receive clean isolated vocals WAV, not raw mixed video
            self.assertEqual(len(transcribe_inputs), 1)
            self.assertTrue(transcribe_inputs[0].endswith("qa/source-vocals.wav"), transcribe_inputs)


if __name__ == "__main__":
    unittest.main()
