# replica-pipeline

Fail-closed backend that forces the `/replica` skill order on every film.

Agents and scripts cannot jump to H3, assemble, or superres without stage artifacts on disk.

## Stages

1. `1_read_source` — source duration, cuts, presenter/place, ASR
2. `2_vo_adapt` — extract-then-adapt VO with density lock
3. `3_beat_plan` — shot list from source cuts / VO beats (4–15s; no fixed-7 template)
4. `4_stills` — identity + size locked first frames + **`SIZE-LOCK.json` (film-level product scale)**
5. `5_h3` — generation + meaning check + **S01 voice lock for later shots**
6. `6_assemble` — captions + BGM
7. `7_superres` — locked until explicit user allow

## Film-level locks

### Voice (S01 → whole film)

- S01: `i2v` + reviewed still.
- After S01 downloads: `python -m replica_pipeline extract-voice FILM` → `qa/voice-lock/S01-voice.wav`.
- **S02+:** `ref2v` with `referenceImagesBase64=[still]` + `referenceAudiosBase64=[S01 wav]`. Local H3 rejects `i2v`+audio (422).
- Use `replica_pipeline.h3_submit.run(...)` from film `submit_one.py`.

### Product size (no shot-to-shot scale drift)

- Stage 4 requires `qa/SIZE-LOCK.json` with `canon_still` and every shot `PASS`.
- Later stills must reference the canon still + product photos; wrong scale = FAIL.

## Install

```bash
pip install -e .
# or run from checkout
python -m replica_pipeline status ./19-室内妈妈讲解
```

## CLI

```bash
python -m replica_pipeline init --batch .
python -m replica_pipeline status FILM_DIR
python -m replica_pipeline require FILM_DIR h3_submit --shot S01
python -m replica_pipeline set FILM_DIR 1_read_source pass
python -m replica_pipeline advance FILM_DIR
python -m replica_pipeline void-sparse7 FILM_DIR
python -m replica_pipeline extract-voice FILM_DIR
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline audit-batch --batch .
```

Exit `2` = gate blocked.

## Hard-gate H3 submit

```python
from pathlib import Path
import sys
OUT = Path(__file__).resolve().parent
# installed package:
from replica_pipeline.h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```

Or with a batch-local `_pipeline/` copy:

```python
sys.path.insert(0, str(OUT.parent / "_pipeline"))
from h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```

## Why

Prose skills get skipped under deadline pressure. This repo turns the skill into a state machine: missing `cuts.json` / VO density / still PASS / size lock / S01 voice → H3 submit exits 2.
