# replica-pipeline

Fail-closed `/replica` 生产闸门：缺产物不能进下一步；H3 / 拼装 / 超分全部硬拦截。

**仓库：** https://github.com/forlovess/replica-pipeline  
**本机目录：** `/Volumes/greatwall/code/replica-pipeline`

## 七步

| 阶段 | 要交的东西 |
| --- | --- |
| `1_read_source` | `qa/source.json` + `cuts.json` + `presenter-place.md` |
| `2_vo_adapt` | `VO-REMAKE.md` + `vo-density.json`（密度闸） |
| `3_beat_plan` | `REBEAT-PLAN.json`（4–15s；禁无依据固定 7 镜） |
| `4_stills` | 每镜 `first-frames/h3/Sxx-first.jpg` + **`STILL-PRODUCT-LOCK.json`** + **`VLM-STILL-REVIEW.json`** + `SIZE-LOCK.json` + `PERSON-LOCK.json` + `still-contact-sheet.jpg`（+ 绑 sha256 的 `still-contact-sheet.json` 清单） |
| `5_h3` | 每镜 `shots/Sxx.mp4` + `H3-MEANING.md` + **`H3-VISUAL-LOCK.json`（4 帧抽检）** + S01 后音色锁 |
| `6_assemble` | 去杂音 + ASR 逐字对齐大字幕（56px@1152）+ BGM（**优先源配乐**）+ `assemble-check.json` |
| `7_superres` | 须用户 `allow-superres` |

## 静帧主路径

Hypit 切源片 → 提每镜首帧 → AI **只换商品**（锁样式 + 大小）→ `first-frames/h3/Sxx-first.jpg`。  
有原镜首帧时不要整图另造场景。

### 源首帧换品 ≠ 抄源包体量

源 t0 只锁：**人 / 场 / 姿势 / 构图框**。  
**商品尺度**只跟用户说明（`user_spec`：cm、上身比例）+ 尺标图 / `worn-scale-ref`。

禁止把源片大号旅行包的 bbox / 占背面积一起带进复刻静帧。  
`SIZE-LOCK` 在 `source=user_spec` 时必须写结构化证据；裸字符串 `"PASS"` **不能**过 `4_stills` / `h3_submit`。

### 进 H3 前的同框对比板（`inspect-stills`）

`inspect-stills` 一次做三件事，全部 fail-closed：

1. **几何检查** — 每镜首帧必须竖屏 ~9:16（宽高比 0.50–0.65）、≥480×800  
2. **同框对比板** — `qa/still-contact-sheet.jpg`：前排放 `SIZE-DIAGRAM` / `WORN-SCALE-REF` / `CANON`，后接 `S01..Sxx` 横排，每格带文字标签  
3. **绑 sha256 清单** — `qa/still-contact-sheet.json` 记录每格来源与 sha256；任一静帧重画后对比板**立即作废**（`Sxx stale`），必须重跑 `inspect-stills`

尺寸/人物/商品三类锁都**禁裸 PASS**，全部是结构化对象并绑 `still_sha256`：

| 锁 | 结构 | 绑定 |
| --- | --- | --- |
| `STILL-PRODUCT-LOCK.json` | `status` + `geometry_ok` / `features_intact` / `no_wrong_variant` / `no_hallucinated_tags` | `still_sha256` |
| `VLM-STILL-REVIEW.json` | 独立 VLM 的 `verdict` + 4 个布尔（`size_match` / `identity_match` / `person_match` / `no_extra_tags`） | `reviewer` 须 `vlm:` 前缀 + `still_sha256` |
| `PERSON-LOCK.json` | `same_person` / `same_outfit` / `same_place` | `still_sha256` |

独立 VLM 复审（`vlm-review-still`）与出图分离：生成方不能自审自签。默认走本机 `codex exec`，也可用 `REPLICA_VLM_URL` / `REPLICA_VLM_KEY` / `REPLICA_VLM_MODEL` 指任意 OpenAI 兼容视觉端点。

## 全片硬锁

1. **人物** — `PERSON-LOCK.json`：定妆后跟本条复刻人物（通常 S01），禁止自由换人换装  
2. **体量** — `SIZE-LOCK.json`：`user_spec`（说了尺寸）或 `first_frame`（没说跟 S01）  
3. **音色** — S01 `i2v` → `qa/voice-lock/S01-voice.wav`；S02+ 必须 `ref2v`（静帧 + S01 wav）  
4. **BGM** — 能抽就用源配乐压在复刻人声下；不行再新曲。口播仍用生成声

### `user_spec` 体量证据（硬）

顶层必填：

- `forbid_source_product_scale: true`
- `physical_cm` / `phrase` / `worn_rule` / `user_spec_text` 至少一项
- 建议：`size_diagram`、`worn_scale_ref`

每镜必须是对象（不能再写 `"S01": "PASS"`）：

```json
{
  "source": "user_spec",
  "forbid_source_product_scale": true,
  "physical_cm": {"w": 26, "h": 27, "d": 13},
  "size_diagram": "_shared/product-lock/front-with-size.jpg",
  "worn_scale_ref": "_shared/product-lock/worn-scale-ref.jpg",
  "shots": {
    "S01": {
      "status": "PASS",
      "mode": "worn",
      "vs_user_spec": "match",
      "vs_source_occupancy": "smaller",
      "refs_used": ["front-with-size.jpg", "worn-scale-ref.jpg"],
      "note": "mini; not source travel-bag footprint"
    }
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `mode` | `worn` / `handheld` / `open` / `detail` / `other` |
| `vs_user_spec` | 必须 `match` |
| `vs_source_occupancy` | `smaller` 或 `n/a`；**禁止** `same` / `larger` |
| `refs_used` | 须能看出尺标图（如 `front-with-size`）；`worn` 还须比例板 |

CLI 写回单镜：

```bash
python -m replica_pipeline size-review FILM_DIR --shot S01 --mode worn \
  --vs-source smaller --vs-spec match \
  --refs front-with-size.jpg,worn-scale-ref.jpg \
  --note "mini chest 1/3–2/5"
```

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
python -m replica_pipeline inspect-stills FILM_DIR             # 首帧宽高比/分辨率检查 + 生成带标签同框对比板（重画静帧后必须重跑）
python -m replica_pipeline product-review FILM_DIR --shot S01 # 结构化商品锁（禁裸 PASS，绑 sha256）
python -m replica_pipeline size-review FILM_DIR --shot S01 --mode worn --vs-source smaller --refs front-with-size.jpg,worn-scale-ref.jpg
python -m replica_pipeline person-review FILM_DIR --shot S02
python -m replica_pipeline vlm-review-still FILM_DIR           # 独立 VLM 复审首帧，过审才能进 H3
python -m replica_pipeline inspect-shots FILM_DIR              # 抽 4 帧联系表 + 扫描开头杂音/长停顿
python -m replica_pipeline vlm-review-video FILM_DIR           # 独立 VLM 复审视频 4 帧联系表
python -m replica_pipeline video-review FILM_DIR --shot S01    # 手动补录视频画面过检（绑 shot_sha256）
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline audit-batch --batch .
python -m replica_pipeline source-prepare FILM_DIR --source src.mp4 [--use-hypit] [--transcribe] # 读源切点/概览图
python -m replica_pipeline estimate-speech --text "词句" --pace fast   # 语速与秒数测算
python -m replica_pipeline lint-prompts FILM_DIR                    # 交 H3 前校验台词/时长/残句/禁言/锚点
python -m replica_pipeline candidate-list FILM_DIR [--shot S01]     # 查看历史重抽候选
python -m replica_pipeline candidate-archive FILM_DIR --shot S01 --clear # 归档保留旧渲染再重跑
python -m replica_pipeline candidate-select FILM_DIR --shot S01 --candidate S01-attempt001
python -m replica_pipeline assemble FILM_DIR --bgm qa/bgm.wav       # 去杂音 + 压Bgm(sidechain) + ASR抖音大字幕拼装
python -m replica_pipeline review-html --batch .                    # 生成带时间戳评论区(FEEDBACK.json)与对比板审片页
python -m replica_pipeline export-hypit FILM_DIR                    # 导出可编辑 Hypit SVML/SVRun 工程
replica-pipeline status FILM_DIR   # console script
```

## 吸收自 Hypit 的工程能力

- **语速预测 + 单一口径 + Prompt 预检（`script_lint`）**：支持 `<display|spoken>` Dual Text、中文/混合音节秒数测算、拦截半截话残句（如「再一个就是」）、`BRIEF-RULES.json` 禁宣称拦截、以及 `<Picture 1>` / `<Audio 1>` / 时长过载预检（并在 `h3_submit` 自动门控）。
- **Run Candidates 归档与回切（`candidates`）**：重抽 H3（`--reroll` 或 `candidate-archive --clear`）自动把旧视频、任务记录、4 帧联系表快照进 `shots/candidates/Sxx/attempt-NNN/`，随时 `candidate-select` 无损切回。
- **声音压低与抖音大字幕拼装（`assemble`）**：语音 `-16 LUFS` 响度均衡 + `sidechaincompress` 对白避让压 BGM + 抖音去尾标点字幕 + 自动裁除开头杂音与静默。
- **交互审片台 + 时间戳评论（`review-html`）**：成片播放器联动 ASR 句点跳转、同框对比板、各锁状态徽章与 `FEEDBACK.json` 时间戳意见导出。
- **Hypit 双向桥接（`hypit_bridge`）**：`source-prepare` 优先走 `hypit media` / `hypit transcribe` 并本地回退；`export-hypit` 一键吐出标准 `@hypit/markup@1` `production.svml` 与 `render.svrun`。

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
