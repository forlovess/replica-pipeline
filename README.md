# replica-pipeline

Fail-closed backend that forces the `/replica` skill order on every film.

## Film-level locks

### Stills preferred path
Hypit cut source → extract each shot t0 → AI **product-replace** on that frame (lock SKU style + size; keep person/place/pose). Do not free-paint a new scene when a source opening frame exists.

### Person (remake continuity — no free casting)
- Cast from `presenter-place.md`, then lock `qa/PERSON-LOCK.json` to the remake canon still (usually S01).
- Later shots must match **that remake person** (hair/outfit/likeness). Not source face-ID; not free improvisation.

### Voice (S01 → whole film)
- S01 `i2v` → extract `qa/voice-lock/S01-voice.wav`
- S02+ `ref2v` = still + S01 wav

### Product size
| source | When | Authority |
| --- | --- | --- |
| `user_spec` | User stated size | That spec globally |
| `first_frame` | No size stated | S01 still globally |

### BGM
Prefer source soundtrack under remake voice when extractable; else new track. Remake spoken track stays generated (not source 口播).

## CLI

```bash
pip install -e .
python -m replica_pipeline status FILM
python -m replica_pipeline extract-voice FILM
python -m replica_pipeline require FILM h3_submit --shot S01
```

Hard submit: `from replica_pipeline.h3_submit import run`
