# replica-pipeline

Fail-closed backend that forces the `/replica` skill order on every film.

## Film-level locks

### Voice (S01 → whole film)

- S01: `i2v` + reviewed still → extract `qa/voice-lock/S01-voice.wav`
- S02+: `ref2v` with still + S01 wav (`i2v`+audio → 422 on local H3)

### Product size (precedence)

| `source` | When | Authority |
| --- | --- | --- |
| `user_spec` | User / Brief stated size (cm, worn fraction, …) | **That spec** for the whole film |
| `first_frame` | No size stated | **S01 accepted still** for the whole film |

Do not invent a third scale. Stage `4_stills` requires `qa/SIZE-LOCK.json` with valid `source` and every shot PASS.


## Install

```bash
pip install -e .
python -m replica_pipeline status ./FILM
```

## CLI

```bash
python -m replica_pipeline init --batch .
python -m replica_pipeline status FILM_DIR
python -m replica_pipeline require FILM_DIR h3_submit --shot S01
python -m replica_pipeline extract-voice FILM_DIR
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline audit-batch --batch .
```

## Hard-gate H3 submit

```python
from pathlib import Path
import sys
OUT = Path(__file__).resolve().parent
from replica_pipeline.h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```

Batch-local copy: put `_pipeline/` next to films and `from h3_submit import run`.
