# Replica pipeline（强制闸门）

把 `/replica` 收成 fail-closed 状态机。缺产物不能进下一步；`submit_one.py` 开跑前必须过闸。

**线上仓库：** https://github.com/forlovess/replica-pipeline  
本目录是批次内副本；以 GitHub 为主，改闸门逻辑先改仓库再同步回来。

## 命令

```bash
PL=_pipeline/pipeline.py
python3 $PL init --batch .
python3 $PL status 19-室内妈妈讲解
python3 $PL require 19-室内妈妈讲解 h3_submit --shot S01   # exit 2 = 拦截
python3 $PL set 19-室内妈妈讲解 1_read_source pass
python3 $PL advance 19-室内妈妈讲解
python3 $PL void-sparse7 19-室内妈妈讲解
python3 $PL extract-voice 19-室内妈妈讲解   # S01 落盘后提取全片音色锁
python3 $PL allow-superres 19-室内妈妈讲解   # 仅用户确认后
python3 $PL audit-batch
```

## 阶段

1. `1_read_source` — `qa/source.json` + `qa/cuts.json` + `qa/presenter-place.md`
2. `2_vo_adapt` — `qa/VO-REMAKE.md` + `qa/vo-density.json`（未标 `facts_exhausted` 时密度须 ≥0.8）
3. `3_beat_plan` — `qa/REBEAT-PLAN.json`（每镜 4–15s；禁止无依据的固定 7 镜模板）
4. `4_stills` — 每镜静帧 + `STILL-PRODUCT-LOCK.md` + **`SIZE-LOCK.json`** + **`PERSON-LOCK.json`（人物连续）**
5. `5_h3` — 每镜 `shots/Sxx.mp4` + `qa/H3-MEANING.md` PASS + **S01 后音色锁**
6. `6_assemble` — captioned + bgm + `assemble-check.json`
7. `7_superres` — 须 `allow-superres`（用户确认）

## 全片硬锁（新增）

### 0) 人物一致性（跟本条复刻人物，禁止自由发挥）

- **开局**：按 `presenter-place.md` 对齐源片的性别/年龄段/发型/服装/场所（不要另造人设）。
- **定妆后**：以本条复刻过审人物静帧（通常 S01）为 `PERSON-LOCK.canon_still`；S02+ **必须跟这个复刻人物一致**，不能每镜自由换脸/换装。
- 程度：跟复刻已定人物连续即可；**不是**像素贴源片脸，也**不是**每镜重新发挥。
- 画 S02+ 静帧时把 **PERSON canon 静帧** 和商品图一起当 reference。

```json
{
  "canon_still": "first-frames/h3/S01-first.jpg",
  "presenter_place_md": "qa/presenter-place.md",
  "traits": {
    "gender": "女",
    "apparent_age": "年轻成人",
    "hair": "浅棕波浪长发+马尾抓夹",
    "outfit": "黑丝绒长袖+黑裙"
  },
  "place": "电梯厅/走廊",
  "shots": {"S01": "PASS", "S02": "PASS"}
}
```

### 1) 人声音色锁（S01 → 全片）

- S01 用 `i2v` + 过审首帧生成；落盘后 `extract-voice`（或 `h3_submit` 自动）写出 `qa/voice-lock/S01-voice.wav`。
- **S02+ 禁止再走纯 i2v**：必须 `ref2v`，`referenceImagesBase64=[本镜静帧]` + `referenceAudiosBase64=[S01 wav]`。
  - 本机 H3：`i2v` 带 `referenceAudiosBase64` → 422；`ref2v` 图+音频 → 接受。
- Prompt 须含 `<Audio 1>` 音色锁句；`submit_one` / `_pipeline/h3_submit.py` 会自动补。
- 闸门：S02+ `require h3_submit` 缺 wav → exit 2。

### 2) 商品体量锁（全片同一尺度）

**权威顺序（二选一，不要混）：**

| `source` | 何时用 | 全片跟谁 |
| --- | --- | --- |
| `user_spec` | 用户 / Brief / PRODUCT-STILL-RULE **说了**尺寸（cm、上身占胸宽比例等） | **说明尺寸**；`canon_still` 只当视觉板，不能另开一套更大/更小 |
| `first_frame` | **没说**尺寸 | **第一帧（S01 过审静帧）**；后续镜必须贴同一体量 |

- 静帧阶段必须写 `qa/SIZE-LOCK.json`：填 `source` + `canon_still` + 每镜 `PASS`。
- 后续静帧：`reference` 带商品实拍 +（上身时）worn-scale-ref + **canon 静帧**；`user_spec` 时还要把说明尺寸写进构图/prompt。
- 忽大忽小 = FAIL，重画静帧。

`user_spec` 示例（本批次粉格美乐蒂）：

```json
{
  "source": "user_spec",
  "canon_still": "first-frames/h3/S01-first.jpg",
  "worn_scale_ref": "_shared/product-lock/worn-scale-ref.jpg",
  "physical_cm": {"w": 26, "h": 27, "d": 13},
  "phrase": "迷你约26×27×13cm；上身包宽约胸宽1/3–2/5",
  "worn_rule": "bag width ~1/3–2/5 of chest; not travel-bag scale",
  "user_spec_text": "PRODUCT-STILL-RULE 26×27×13 + worn 1/3–2/5",
  "shots": {"S01": "PASS", "S02": "PASS"}
}
```

`first_frame` 示例（用户未给尺寸）：

```json
{
  "source": "first_frame",
  "canon_still": "first-frames/h3/S01-first.jpg",
  "phrase": "match S01 still product scale against body/hands",
  "shots": {"S01": "PASS", "S02": "PASS"}
}
```

## 硬拦截

各片 `submit_one.py` 委托 `_pipeline/h3_submit.py`：

```python
from pathlib import Path
import sys
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parent / "_pipeline"))
from h3_submit import run
raise SystemExit(run(OUT, sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0))
```

无 `4_stills` PASS（及前置阶段）→ 直接 `SystemExit`。  
无 `SIZE-LOCK.json` /（S02+）无音色 wav → 同样拦截。
