# replica-pipeline

Fail-closed backend that forces the `/replica` skill order on every film.

Agents and scripts cannot jump to H3, assemble, or superres without stage artifacts on disk.

## Stages

1. `1_read_source` — source duration, cuts, presenter/place, ASR
2. `2_vo_adapt` — extract-then-adapt VO with density lock
3. `3_beat_plan` — shot list from source cuts / VO beats (4–15s; no fixed-7 template)
4. `4_stills` — identity + size locked first frames
5. `5_h3` — generation + meaning check
6. `6_assemble` — captions + BGM
7. `7_superres` — locked until explicit user allow

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
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline audit-batch --batch .
```

Exit `2` = gate blocked.

## Hard-gate H3 submit

At the top of your film `submit_one.py` (after `OUT` / `SHOT` are set):

```python
import sys
from pathlib import Path
# if installed:
from replica_pipeline.gate import require
require(OUT, "h3_submit", shot=SHOT)
```

Or point `PYTHONPATH` at this repo and import `replica_pipeline.gate`.

## Why

Prose skills get skipped under deadline pressure. This repo turns the skill into a state machine: missing `cuts.json` / VO density / still PASS → H3 submit exits 2.
