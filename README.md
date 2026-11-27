# replica-pipeline

Fail-closed `/replica` 生产闸门：缺产物不能进下一步；H3 / 拼装 / 超分全部硬拦截。

**仓库：** https://github.com/forlovess/replica-pipeline

## 七步

| 阶段 | 要交的东西 |
| --- | --- |
| `1_read_source` | `qa/source.json` + `cuts.json` + `presenter-place.md` |
| `2_vo_adapt` | `VO-REMAKE.md` + `vo-density.json`（密度闸） |
| `3_beat_plan` | `REBEAT-PLAN.json`（4–15s；禁无依据固定 7 镜） |
| `4_stills` | 每镜 `first-frames/h3/Sxx-first.jpg` + `STILL-PRODUCT-LOCK.md` + `SIZE-LOCK.json` + `PERSON-LOCK.json` |
| `5_h3` | 每镜 `shots/Sxx.mp4` + `H3-MEANING.md` + S01 后音色锁 |
| `6_assemble` | captioned + BGM（**优先源配乐**）+ `assemble-check.json` |
| `7_superres` | 须用户 `allow-superres` |

## 静帧主路径

Hypit 切源片 → 提每镜首帧 → AI **只换商品**（锁样式 + 大小）→ `first-frames/h3/Sxx-first.jpg`。  
有原镜首帧时不要整图另造场景。

## 全片硬锁

1. **人物** — `PERSON-LOCK.json`：定妆后跟本条复刻人物（通常 S01），禁止自由换人换装  
2. **体量** — `SIZE-LOCK.json`：`user_spec`（说了尺寸）或 `first_frame`（没说跟 S01）  
3. **音色** — S01 `i2v` → `qa/voice-lock/S01-voice.wav`；S02+ 必须 `ref2v`（静帧 + S01 wav）  
4. **BGM** — 能抽就用源配乐压在复刻人声下；不行再新曲。口播仍用生成声

## 安装

```bash
git clone https://github.com/forlovess/replica-pipeline.git
cd replica-pipeline
pip install -e .
```

或把本仓库拷到批次旁作 `_pipeline/`（flat import 同样可用）。

## CLI

```bash
python -m replica_pipeline init --batch .
python -m replica_pipeline status FILM_DIR
python -m replica_pipeline require FILM_DIR h3_submit --shot S01
python -m replica_pipeline advance FILM_DIR
python -m replica_pipeline extract-voice FILM_DIR
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline audit-batch --batch .
replica-pipeline status FILM_DIR   # console script
```

Exit `2` = 闸门拦截。

## 片子里怎么交 H3

```python
from pathlib import Path
import sys
OUT = Path(__file__).resolve().parent
from replica_pipeline.h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```

批次内 `_pipeline/` 副本：

```python
sys.path.insert(0, str(OUT.parent / "_pipeline"))
from h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```
