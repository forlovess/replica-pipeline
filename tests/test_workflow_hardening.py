import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from replica_pipeline import assemble_tools, asr_verify, h3_submit, hypit_bridge, matrix_audit, pipeline, script_lint, still_qa, superres, vlm_review, voice_lock


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False))


def make_video(path, duration, frequency=440, color='red'):
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=144x256:r=25:d={duration}',
                    '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={duration}', '-c:v', 'libx264', '-c:a', 'aac',
                    '-metadata', 'comment=generator signature', '-shortest', str(path)], check=True)


class ScriptContractTests(unittest.TestCase):
    def test_dual_projection(self):
        result = script_lint.compile_shot_text('<HOST><d>只要<99元|九十九元>。</d>')
        self.assertEqual(result['display'], '只要99元。')
        self.assertEqual(result['spoken'], '只要九十九元。')
        self.assertEqual(result['role'], 'HOST')

    def test_malformed_dialogue(self):
        with self.assertRaises(ValueError):
            script_lint.compile_shot_text('</d><d>你好')

    def test_generic_hook(self):
        self.assertTrue(script_lint.lint_hook({'spoken': '大家好，来看这个包。'}))
        self.assertFalse(script_lint.lint_hook({'spoken': '大家好，来看这个包。', 'allow_generic_hook': True,
                                               'allow_generic_hook_reason': 'Required series intro'}))
        self.assertTrue(script_lint.lint_hook({'spoken': '先看做工。', 'hook_start_sec': 3.1}))

    def test_upstream_lock_rejects_source_and_script_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            qa = film / 'qa'
            qa.mkdir()
            (film / 'prompts').mkdir()
            (film / 'source.mp4').write_bytes(b'source')
            write_json(qa / 'source.json', {'path': 'source.mp4', 'duration': 6, 'cuts_count': 2})
            write_json(qa / 'cuts.json', {'cuts': [{'t': 1}, {'t': 3}]})
            (qa / 'presenter-place.md').write_text('Presenter and location')
            self.assertIn('silent_source', pipeline.validate_source(film)[0])
            write_json(qa / 'source-asr.json', {'segments': [{'text': '先看做工。'}]})
            (qa / 'VO-REMAKE.md').write_text('# VO\nS01: 先看做工。\n')
            write_json(qa / 'vo-density.json', {'source_chars': 4, 'remake_chars': 4, 'ratio': 1})
            plan = {'source_basis': 'scene_merge', 'shots': [{'id': 'S01', 'sec': 4, 'spoken': '先看做工。'}]}
            write_json(qa / 'REBEAT-PLAN.json', plan)
            prompt = film / 'prompts/S01.txt'
            prompt.write_text('<Picture 1><d>先看做工。</d>')
            pipeline.save(film, pipeline.blank_state(film.name))
            for stage in pipeline.STAGE_ORDER[:3]:
                self.assertEqual(pipeline.cmd_set(argparse.Namespace(film=str(film), stage=stage, status='pass', note='')), 0)
            args = argparse.Namespace(film=str(film), before='stills', shot='S01')
            self.assertEqual(pipeline.cmd_preflight(args), 0)
            prompt.write_text('<Picture 1> new camera move <d>先看做工。</d>')
            self.assertEqual(pipeline.cmd_preflight(args), 2)
            prompt.write_text('<Picture 1><d>先看做工。</d>')
            (qa / 'BRIEF-RULES.json').write_text('{oops}')
            with self.assertRaisesRegex(ValueError, 'BRIEF-RULES'):
                script_lint.compile_script_contract(film)
            (qa / 'BRIEF-RULES.json').unlink()
            write_json(qa / 'cuts.json', {'cuts': [{'t': 1}, {'t': 4}]})
            self.assertEqual(pipeline.cmd_preflight(args), 2)
            write_json(qa / 'vo-density.json', {'source_chars': 99, 'remake_chars': 99, 'ratio': 1})
            self.assertIn('expected source_chars=4', pipeline.validate_vo_density(film)[0])

    def test_review_detects_changed_and_new_reference_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            (film / 'qa').mkdir()
            (film / 'first-frames/h3').mkdir(parents=True)
            still = film / 'first-frames/h3/S01-first.jpg'
            still.write_bytes(b'still')
            board = film / still_qa.STILL_SHEET_NAME
            board.write_bytes(b'board')
            diagram = film / 'diagram.jpg'
            diagram.write_bytes(b'diagram')
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            write_json(film / still_qa.SHEET_MANIFEST_NAME, {'sheet_sha256': digest(board), 'stills': {'S01': digest(still)},
                       'cells': [{'label': 'SIZE-DIAGRAM', 'source_sha256': digest(diagram)}]})
            spec = film / 'qa/SIZE-LOCK.json'
            write_json(spec, {'size_diagram': diagram.name})
            shots = [{'id': 'S01'}]
            self.assertEqual(still_qa.validate_still_sheet(film, shots), [])
            diagram.write_bytes(b'new')
            self.assertIn('SIZE-DIAGRAM changed', still_qa.validate_still_sheet(film, shots)[0])
            worn = film / 'worn.jpg'
            worn.write_bytes(b'worn')
            write_json(spec, {'size_diagram': diagram.name, 'worn_scale_ref': worn.name})
            report = {'shots': {'S01': {'reviewer': 'vlm:codex', 'verdict': 'PASS', 'still_sha256': digest(still),
                                       'size_match': True, 'identity_match': True, 'person_match': True,
                                       'no_extra_tags': True, 'refs_sha256': {'size_diagram': digest(diagram)}}}}
            self.assertTrue(any("new reference 'worn_scale_ref'" in msg for msg in vlm_review.validate_vlm_still_review_data(report, film, shots)))


class ShotMeaningTests(unittest.TestCase):
    def test_asr_detects_substitutions_omissions_and_tampering(self):
        expected = '只要九十九元，包内有拉链，不能水洗。'
        for bad in ('只要九十八元，包内有拉链，不能水洗。', '只要99元，包内有磁扣，不能水洗。',
                    '只要99元，不能水洗。', '只要99元，包内有拉链，能水洗。', '包内有拉链，只要99元，不能水洗。'):
            with self.subTest(bad=bad):
                self.assertEqual(asr_verify.compare_meaning(expected, bad)['verdict'], 'FAIL')
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            for folder in ('shots', 'prompts', 'qa/asr'):
                (film / folder).mkdir(parents=True)
            (film / 'shots/S01.mp4').write_bytes(b'shot')
            (film / 'prompts/S01.txt').write_text('<HOST><d>只要<99元|九十九元>，包内有拉链，不能水洗。</d>')
            asr_path = film / 'qa/asr/S01.json'
            write_json(asr_path, {'text': '只要99元，包内有拉链，不能水洗。'})
            self.assertEqual(asr_verify.verify_shot_meaning(film, 'S01')['verdict'], 'PASS')
            self.assertEqual(asr_verify.validate_meaning_lock_data(film, ['S01']), [])
            asr_path.write_text(asr_path.read_text() + '\n')
            self.assertIn('stale', asr_verify.validate_meaning_lock_data(film, ['S01'])[0])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_failed_acceptance_preserves_task_and_does_not_publish_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            for folder in ('shots', 'prompts', 'qa/asr', 'tasks'):
                (film / folder).mkdir(parents=True)
            make_video(film / 'shots/S01.mp4', 4)
            (film / 'prompts/S01.txt').write_text('<Picture 1><d>这个包好看。</d>')
            write_json(film / 'qa/REBEAT-PLAN.json', {'shots': [{'id': 'S01', 'sec': 4, 'spoken': '这个包好看。'}]})
            task = film / 'tasks/S01.task.json'
            write_json(task, {'task_id': 'same-id', 'state': 'NEEDS_ACCEPTANCE'})
            asr = film / 'qa/asr/S01.json'
            write_json(asr, {'text': '这个包好看。'})
            with mock.patch.object(h3_submit, 'pipeline_require'), mock.patch.object(h3_submit, 'extract_shot_contact_sheet', side_effect=OSError('sheet failed')):
                with self.assertRaisesRegex(OSError, 'sheet failed'):
                    h3_submit.accept_downloaded_shot(film, 'S01')
            self.assertFalse(voice_lock.voice_meta_path(film).exists())
            self.assertEqual(json.loads(task.read_text())['state'], 'NEEDS_ACCEPTANCE')
            with mock.patch.object(h3_submit, 'pipeline_require'):
                result = h3_submit.accept_downloaded_shot(film, 'S01')
            self.assertTrue(result['ok'])
            self.assertTrue(voice_lock.voice_lock_path(film).is_file())
            self.assertEqual(json.loads(task.read_text())['task_id'], 'same-id')


class AssemblyTests(unittest.TestCase):
    def test_lookahead_and_timeline_validation(self):
        graph = assemble_tools.build_ducking_filter(total_dur=7, lookahead_ms=190)
        self.assertEqual(graph.count('adelay=190'), 2)
        self.assertIn('[bgm_delayed][vo_detect]sidechaincompress=', graph)
        self.assertIn('release=800', graph)
        self.assertIn('atrim=start=0.190000:end=7.190000', graph)
        shots = [{'id': 'S01', 'sec': 4}, {'id': 'S02', 'sec': 4, 'audio_owner': 'S01', 'audio_in': 4, 'audio_out': 8}]
        result = assemble_tools.build_assembly_timeline(Path('.'), shots, durations={'S01': 8, 'S02': 4})
        self.assertEqual([(row['audio_in'], row['audio_out']) for row in result['audio']], [(0.0, 8.0)])
        with self.assertRaisesRegex(ValueError, 'out-of-range'):
            assemble_tools.build_assembly_timeline(Path('.'), shots, durations={'S01': 7, 'S02': 4})
        shots[0]['sec'] = 8
        result = assemble_tools.build_assembly_timeline(Path('.'), shots, durations={'S01': 8, 'S02': 4})
        self.assertEqual(result['duration'], 8)
        shots[0]['audio_owner'] = 'S02'
        with self.assertRaisesRegex(ValueError, 'earlier'):
            assemble_tools.build_assembly_timeline(Path('.'), shots, durations={'S01': 8, 'S02': 4})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_spanning_audio_and_dual_caption_across_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            for folder in ('shots', 'prompts', 'qa/asr'):
                (film / folder).mkdir(parents=True)
            make_video(film / 'shots/S01.mp4', 8, 440)
            make_video(film / 'shots/S02.mp4', 4, 1500, 'blue')
            write_json(film / 'qa/REBEAT-PLAN.json', {'shots': [
                {'id': 'S01', 'sec': 4, 'audio_source_sec': 8, 'spoken': '<99元|九十九元>'},
                {'id': 'S02', 'sec': 4, 'audio_owner': 'S01', 'audio_in': 4, 'audio_out': 8},
            ]})
            write_json(film / 'qa/asr/S01.json', {'segments': [{'words': [
                {'word': '99', 'start': 3.55, 'end': 4.16}, {'word': '元', 'start': 4.16, 'end': 4.45},
            ]}]})
            with mock.patch.object(assemble_tools, 'burn_subtitles', side_effect=lambda src, cues, dest, **opts: assemble_tools._clean_remux(src, dest)):
                output = assemble_tools.assemble_film(film, skip_gate=True, width=144, height=256, matrix_seed='demo')
            cues = json.loads((film / 'qa/captions.asr-cues.json').read_text())
            self.assertEqual(cues[0]['text'], '99元')
            self.assertLess(cues[0]['start'], 4)
            self.assertGreater(cues[0]['end'], 4)
            self.assertEqual(output['assemble_check']['matrix_variation']['seed'], 'demo')
            cut = film / 'qa/strict-with-bgm.mp4'
            audio = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '5', '-i', str(cut), '-t', '1', '-vn',
                                             '-ac', '1', '-ar', '8000', '-f', 's8', '-'])
            signs = [sample > 127 for sample in audio]
            crossings = sum(left != right for left, right in zip(signs, signs[1:]))
            self.assertTrue(800 <= crossings <= 960, crossings)
            metadata = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=comment', '-of', 'json', str(cut)])
            self.assertNotIn(b'generator signature', metadata)
            self.assertIn('-map_metadata', assemble_tools.STRIP_METADATA)
            hypit = hypit_bridge.export_hypit_project(film)
            svml = Path(hypit['svml']).read_text()
            self.assertIn('id="audio-1" from="w-s01.start" for="8s"', svml)
            self.assertNotIn('a-s02', svml)
            if shutil.which('hypit'):
                for path in ('svml', 'svrun'):
                    subprocess.run(['hypit', 'check', str(Path(hypit[path]).name)], cwd=hypit['project_dir'], capture_output=True, check=True)


class MatrixAndSuperresTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_stage_seven_verifies_real_media_and_detects_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            (film / 'qa').mkdir()
            source, output = film / 'qa/captioned.mp4', film / f'{film.name}.mp4'
            for path, size in ((source, '432x768'), (output, '864x1536')):
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=red:s={size}:d=0.16',
                                '-c:v', 'libx264', str(path)], check=True)
            allow = film / 'qa/superres-allow.json'
            write_json(allow, {'allow': True, 'by': 'user'})
            self.assertFalse(pipeline.artifacts_present(film, '7_superres')[0])
            write_json(film / 'qa/SUPERRES.json', {'status': 'PASS', 'scale': 2, 'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'output_bytes': output.stat().st_size})
            self.assertEqual(pipeline.artifacts_present(film, '7_superres'), (True, []))
            output.write_bytes(b'tampered')
            self.assertFalse(pipeline.artifacts_present(film, '7_superres')[0])

    def test_matrix_duplicate_detection_and_bgm_override(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            for name in ('01-one', '02-two'):
                film = batch / name
                (film / 'qa').mkdir(parents=True)
                (film / 'first-frames/h3').mkdir(parents=True)
                (film / 'qa/bgm.wav').write_bytes(b'shared')
                (film / 'first-frames/h3/S01-first.jpg').write_bytes(b'canon')
                (film / 'qa/VO-REMAKE.md').write_text('先看做工，走线平整又耐磨。')
            first = matrix_audit.audit_matrix([batch])
            self.assertFalse(first['ok'])
            self.assertEqual({row['type'] for row in first['findings']}, {'bgm', 'canon_still', 'vo', 'hook'})
            policy = batch / 'matrix-policy.json'
            write_json(policy, {'allowed_bgm_sha256': [hashlib.sha256(b'shared').hexdigest()]})
            (batch / '02-two/first-frames/h3/S01-first.jpg').write_bytes(b'different')
            (batch / '02-two/qa/VO-REMAKE.md').write_text('背带调起来很方便。')
            self.assertTrue(matrix_audit.audit_matrix(batch, policy_path=policy)['ok'])
            self.assertIn('Allowed: bgm', (batch / 'MATRIX-AUDIT.md').read_text())

    def test_superres_chunks_resume_and_verified_download(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            (film / 'qa').mkdir()
            (film / 'qa/captioned.mp4').write_bytes(b'0123456789')
            task = '12345678-1234-4321-8234-123456789012'
            output = b'verified upscale bytes'
            sha = hashlib.sha256(output).hexdigest()
            seen, uploaded = [], bytearray()
            running, fail_upload, bad_download = False, True, True

            def request(request, timeout):
                nonlocal running, fail_upload
                method, url = request.get_method(), request.full_url
                self.assertEqual(request.get_header('X-api-key'), 'secret')
                self.assertEqual(request.get_header('User-agent'), superres.USER_AGENT)
                seen.append((method, url))
                if url.endswith('/v1/upscales'):
                    self.assertEqual(json.loads(request.data), {'filename': 'captioned.mp4', 'scale': 2})
                    data = {'taskId': task}
                elif '/input?' in url:
                    offset = int(url.split('=')[-1])
                    self.assertEqual(request.get_header('Content-type'), 'application/octet-stream')
                    self.assertEqual(offset, len(uploaded))
                    if offset == 4 and fail_upload:
                        fail_upload = False
                        raise OSError('upload lost')
                    uploaded.extend(request.data)
                    return io.BytesIO(json.dumps({'data': {'uploadedBytes': len(uploaded)}}).encode())
                elif url.endswith('/start'):
                    self.assertEqual(request.get_header('Content-type'), 'application/json')
                    self.assertEqual(request.data, b'')
                    running = True
                    return io.BytesIO()
                elif url.endswith('/file'):
                    return io.BytesIO(output[:-1] + b'X' if bad_download else output)
                elif running:
                    running = False
                    data = {'status': 'RUNNING'}
                elif len(uploaded) == 10:
                    data = {'status': 'COMPLETED', 'output': {'sha256': sha, 'bytes': len(output)}}
                else:
                    data = {'status': 'UPLOADING', 'uploadedBytes': len(uploaded)}
                return io.BytesIO(json.dumps({'data': data}).encode())

            with (mock.patch.object(superres, 'require') as gate,
                  mock.patch.object(superres.urllib.request, 'urlopen', side_effect=request),
                  mock.patch.object(superres, 'CHUNK_BYTES', 4),
                  mock.patch.object(superres, '_verify_video', return_value={'width': 1152, 'height': 2048}),
                  mock.patch.dict(os.environ, {'SUPERRES_API_KEY': 'secret', 'SUPERRES_BASE_URL': 'http://localhost:9999'})):
                with self.assertRaisesRegex(OSError, 'upload lost'):
                    superres.run_superres(film, poll_interval=0)
                self.assertEqual(json.loads((film / 'qa/superres.task.json').read_text())['task_id'], task)
                with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                    superres.run_superres(film, resume=True, poll_interval=0)
                self.assertFalse((film / f'{film.name}.mp4').exists())
                bad_download = False
                result = superres.run_superres(film, resume=True, poll_interval=0)
                self.assertEqual(superres.run_superres(film, resume=True, poll_interval=0), result)
                gate.assert_called_with(film.resolve(), 'superres')
            self.assertEqual(result['output_sha256'], sha)
            self.assertEqual((film / f'{film.name}.mp4').read_bytes(), output)
            self.assertEqual(bytes(uploaded), b'0123456789')
            self.assertEqual(sum(method == 'POST' and url.endswith('/v1/upscales') for method, url in seen), 1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_superres_copies_source_audio_onto_silent_upscale(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            (film / 'qa').mkdir()
            source = film / 'qa/captioned.mp4'
            silent = film / 'qa/silent-upscale.mp4'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=432x768:d=0.2',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.2', '-c:v', 'libx264', '-c:a', 'aac',
                            '-shortest', str(source)], check=True)
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=864x1536:d=0.2',
                            '-c:v', 'libx264', '-an', str(silent)], check=True)
            payload = silent.read_bytes()
            sha = hashlib.sha256(payload).hexdigest()
            task = '12345678-1234-4321-8234-123456789012'
            uploaded = bytearray()

            def request(request, timeout):
                method, url = request.get_method(), request.full_url
                if method == 'POST' and url.endswith('/v1/upscales'):
                    data = {'taskId': task}
                elif '/input?' in url:
                    uploaded.extend(request.data)
                    return io.BytesIO(json.dumps({'data': {'uploadedBytes': len(uploaded)}}).encode())
                elif url.endswith('/start'):
                    return io.BytesIO()
                elif url.endswith('/file'):
                    return io.BytesIO(payload)
                elif len(uploaded) == source.stat().st_size:
                    data = {'status': 'COMPLETED', 'output': {'sha256': sha, 'bytes': len(payload)}}
                else:
                    data = {'status': 'UPLOADING', 'uploadedBytes': len(uploaded)}
                return io.BytesIO(json.dumps({'data': data}).encode())

            with (mock.patch.object(superres, 'require'),
                  mock.patch.object(superres.urllib.request, 'urlopen', side_effect=request),
                  mock.patch.dict(os.environ, {'SUPERRES_API_KEY': 'secret', 'SUPERRES_BASE_URL': 'http://localhost:9999'})):
                result = superres.run_superres(film, poll_interval=0)
            final = film / f'{film.name}.mp4'
            self.assertTrue(result['audio_remuxed'])
            self.assertEqual(result['download_sha256'], sha)
            self.assertEqual(result['output_sha256'], hashlib.sha256(final.read_bytes()).hexdigest())
            streams = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
                                                '-of', 'csv=p=0', str(final)], text=True)
            self.assertIn('video', streams)
            self.assertIn('audio', streams)


class RoleVoiceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_host_and_guest_banks_stay_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            film = Path(directory)
            for folder in ('shots', 'qa', 'prompts'):
                (film / folder).mkdir()
            specs = [{'id': 'S01', 'role': 'HOST'}, {'id': 'S02'}, {'id': 'S03', 'role': 'GUEST'}, {'id': 'S04', 'role': 'HOST'}]
            write_json(film / 'qa/REBEAT-PLAN.json', {'shots': specs})
            guest_prompt = '<Picture 1><GUEST><d>只要<99元|九十九元>。</d>'
            (film / 'prompts/S02.txt').write_text(guest_prompt)
            make_video(film / 'shots/S01.mp4', 1, 440)
            make_video(film / 'shots/S02.mp4', 1, 880)
            frame = film / 'frame.jpg'
            frame.write_bytes(b'image')
            host = voice_lock.extract_voice_from_shot(film)
            self.assertEqual(host['wav'], 'qa/voice-lock/S01-voice.wav')
            guest_payload, guest_meta = h3_submit.build_payload(film, 'S02', guest_prompt, frame, 4)
            self.assertEqual(guest_meta['mode'], 'i2v')
            self.assertIn('九十九元', guest_payload['prompt'])
            self.assertNotIn('<GUEST>', guest_payload['prompt'])
            self.assertNotEqual(guest_meta['prompt_sha256'], guest_meta['raw_prompt_sha256'])
            with self.assertRaisesRegex(ValueError, 'missing GUEST'):
                h3_submit.build_payload(film, 'S03', guest_prompt, frame, 4)
            guest = voice_lock.extract_voice_from_shot(film, role='GUEST')
            _, meta = h3_submit.build_payload(film, 'S03', guest_prompt, frame, 4)
            self.assertEqual(meta['voice_sha256'], guest['wav_sha256'])
            _, meta = h3_submit.build_payload(film, 'S04', '<Picture 1>', frame, 4)
            self.assertEqual(meta['voice_sha256'], host['wav_sha256'])
            self.assertNotEqual(host['wav_sha256'], guest['wav_sha256'])
            self.assertEqual(json.loads(voice_lock.voice_meta_path(film).read_text())['source_shot'], 'S01')
