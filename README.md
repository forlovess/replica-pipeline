# replica-pipeline

Fail-closed `/replica` 生产闸门：缺产物不能进下一步；H3 / 拼装 / 超分全部硬拦截。

**仓库：** https://github.com/forlovess/replica-pipeline  
**本机目录：** `/Volumes/greatwall/code/replica-pipeline`

## 七步

| 阶段 | 要交的东西 |
| --- | --- |
| `1_read_source` | `qa/source.json` + `cuts.json` + `presenter-place.md` + `source-asr.json`（无语音须显式 `silent_source: true`） |
| `2_vo_adapt` | `VO-REMAKE.md` + `vo-density.json`（密度闸） |
| `3_beat_plan` | `REBEAT-PLAN.json`（4–15s；禁无依据固定 7 镜）；过闸生成 `qa/SCRIPT-LOCK.json` |
| `4_stills` | 每镜 `first-frames/h3/Sxx-first.jpg` + **`STILL-PRODUCT-LOCK.json`** + **`VLM-STILL-REVIEW.json`** + `SIZE-LOCK.json` + `PERSON-LOCK.json` + `still-contact-sheet.jpg`（+ 绑 sha256 的 `still-contact-sheet.json` 清单） |
| `5_h3` | 每镜 `shots/Sxx.mp4` + `accept-shot` 回执 + `H3-MEANING.json` + **`H3-VISUAL-LOCK.json`（4 帧抽检）** + 分角色音色锁 |
| `6_assemble` | 去杂音 + ASR 逐字对齐大字幕（56px@1152）+ BGM（**优先源配乐**）+ `assemble-check.json` |
| `7_superres` | 用户 `allow-superres` + `qa/SUPERRES.json` + `<film_name>.mp4`（验证过的 2× 超分） |

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
3. **音色** — 每个角色首个口播镜验收后提取音色；同角色后续镜必须 `ref2v`（静帧 + 该角色 WAV）。默认 `HOST`，保留 `qa/voice-lock/S01-voice.wav`。
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
python -m replica_pipeline require FILM_DIR stills
python -m replica_pipeline preflight FILM_DIR --before stills|h3|assemble [--shot S01]
python -m replica_pipeline advance FILM_DIR
python -m replica_pipeline asr-verify FILM_DIR --shot S01 [--asr-path FILE] [--language zh]
python -m replica_pipeline accept-shot FILM_DIR --shot S01 [--asr-path FILE] [--vlm-provider none|auto|codex|openai]
python -m replica_pipeline extract-voice FILM_DIR [--role HOST] [--shot S01] [--force]
python -m replica_pipeline inspect-stills FILM_DIR             # 首帧宽高比/分辨率检查 + 生成带标签同框对比板（重画静帧后必须重跑）
python -m replica_pipeline product-review FILM_DIR --shot S01 # 结构化商品锁（禁裸 PASS，绑 sha256）
python -m replica_pipeline size-review FILM_DIR --shot S01 --mode worn --vs-source smaller --refs front-with-size.jpg,worn-scale-ref.jpg
python -m replica_pipeline person-review FILM_DIR --shot S02
python -m replica_pipeline vlm-review-still FILM_DIR           # 独立 VLM 复审首帧，过审才能进 H3
python -m replica_pipeline inspect-shots FILM_DIR              # 抽 4 帧联系表 + 扫描开头杂音/长停顿
python -m replica_pipeline vlm-review-video FILM_DIR           # 独立 VLM 复审视频 4 帧联系表
python -m replica_pipeline video-review FILM_DIR --shot S01    # 手动补录视频画面过检（绑 shot_sha256）
python -m replica_pipeline allow-superres FILM_DIR
python -m replica_pipeline superres FILM_DIR [--base-url URL] [--resume]
python -m replica_pipeline audit-batch --batch .
python -m replica_pipeline source-prepare FILM_DIR --source src.mp4 [--extract-bgm] [--use-hypit] [--transcribe]
python -m replica_pipeline extract-bgm FILM_DIR --source src.mp4 [--engine auto|demucs|audio-separator|center-cancel] # 剥离原片人声与可用BGM
python -m replica_pipeline estimate-speech --text "词句" --pace fast   # 语速与秒数测算
python -m replica_pipeline lint-prompts FILM_DIR                    # 交 H3 前校验台词/时长/残句/禁言/锚点
python -m replica_pipeline candidate-list FILM_DIR [--shot S01]     # 查看历史重抽候选
python -m replica_pipeline candidate-archive FILM_DIR --shot S01 --clear # 归档保留旧渲染再重跑
python -m replica_pipeline candidate-select FILM_DIR --shot S01 --candidate S01-attempt001
python -m replica_pipeline assemble FILM_DIR --bgm qa/bgm.wav       # 去杂音 + 压Bgm(sidechain) + ASR抖音大字幕拼装
python -m replica_pipeline assemble FILM_DIR --matrix-seed film-01 # 可选：可复现的轻微画面差异
python -m replica_pipeline audit-matrix --batch BATCH [--batch ANOTHER_BATCH] [--policy FILE]
python -m replica_pipeline review-html --batch .                    # 生成带时间戳评论区(FEEDBACK.json)与对比板审片页
python -m replica_pipeline export-hypit FILM_DIR                    # 导出可编辑 Hypit SVML/SVRun 工程
replica-pipeline status FILM_DIR   # console script
```

## 吸收自 Hypit 的工程能力

- **原片人声 / BGM 剥离与自动接管（`audio_stem`）**：`extract-bgm`（或 `source-prepare --extract-bgm`）优先走 Demucs（`htdemucs --two-stems=vocals`）或 `audio-separator` 把原片拆成 `qa/source-vocals.wav` + `qa/bgm.wav`，实测伴奏有效响度（空人声干片不会硬留底噪），随后 `assemble` 自动用 H3 新配音并把原片 BGM 用 `sidechaincompress` 压在底下（无配音新片则直接铺原 BGM）。
- **语速预测 + 单一口径 + Prompt 预检（`script_lint`）**：支持 `<display|spoken>` Dual Text、中文/混合音节秒数测算、拦截半截话残句（如「再一个就是」）、`BRIEF-RULES.json` 禁宣称拦截、以及 `<Picture 1>` / `<Audio 1>` / 时长过载预检（并在 `h3_submit` 自动门控）。
- **Run Candidates 归档与回切（`candidates`）**：重抽 H3（`--reroll` 或 `candidate-archive --clear`）自动把旧视频、任务记录、4 帧联系表快照进 `shots/candidates/Sxx/attempt-NNN/`，随时 `candidate-select` 无损切回。
- **声音压低与抖音大字幕拼装（`assemble`）**：语音 `-16 LUFS` 响度均衡 + `sidechaincompress` 对白避让压 BGM + 抖音去尾标点字幕 + 自动裁除开头杂音与静默。
- **交互审片台 + 时间戳评论（`review-html`）**：成片播放器联动 ASR 句点跳转、同框对比板、各锁状态徽章与 `FEEDBACK.json` 时间戳意见导出。
- **Hypit 双向桥接（`hypit_bridge`）**：`source-prepare` 优先走 `hypit media` / `hypit transcribe` 并本地回退；`export-hypit` 一键吐出标准 `@hypit/markup@1` `production.svml` 与 `render.svrun`。

Exit `2` = 闸门拦截。

## 上游锁与返工

`advance`（或 `set … pass`）记录当前证据的哈希。`preflight` / `require` 重新验证所有前置阶段；改动源片、台词、参考图、静帧或视频后，旧 `PASS` 不能替代复审。改动上游证据并重新通过该阶段后，下游阶段恢复待验。旧项目按顺序重新过闸；仅有 `H3-MEANING.md` 的历史项目需为每镜重新跑 `accept-shot`，并保证视觉锁与当前视频一致。

- `source.json` 记录源视频 `path`、正数 `duration` 和 `cuts_path` / `asr_path`（默认 `qa/cuts.json` / `qa/source-asr.json`）。切点用 `{"cuts": [{"t": 1.25}]}` 表示，须递增且在片长内。密度按 ASR 与重制稿实际口播的字母、数字、汉字计数（不计空格标点）；`ratio = remake_chars / source_chars`，明确无源语音时填 `0`。
- `VO-REMAKE.md` 可用纯口播、`S01: …` 逐镜行，或 `<d>…</d>` 段。`REBEAT-PLAN.json` 的 `spoken` 和 H3 prompt 的 `<d>…</d>` 必须一致。`<99元|九十九元>` 向 H3/ASR 用右侧、向字幕用左侧。`role` 或 `<HOST>` / `<GUEST>` 指定说话人；不要给借用别镜音轨的 B-roll 写新台词。S01 钩子要在 3 秒内落地；通用开场必须写 `allow_generic_hook: true` 与 `allow_generic_hook_reason`。静音开场需 `silent: true`、视觉 `hook` 与 `hook_end_sec`。
- H3 下载后自动验收：完整解码、时长、WhisperX 文本、音频杂音及新 4 帧表。若停在 `NEEDS_ACCEPTANCE`，修复证据后跑 `accept-shot`，不会重复 POST，也不会提前发布音色。ASR 优先复用 `qa/asr/Sxx.json`，缺失时用 `hypit transcribe`；可显式给现有 ASR 文件，字幕需要词级时间戳。`--vlm-provider none` 不运行 VLM，但第 5 步仍要求独立视觉锁。

## 跨镜音轨、矩阵与超分

- A-roll 例：S01 `{"sec":4,"audio_source_sec":8}`，S02 `{"sec":4,"audio_owner":"S01","audio_in":4,"audio_out":8}`。前 4 秒取 S01 画面，后 4 秒取 S02 画面，8 秒音频都来自 S01，字幕由 S01 的 ASR 贯穿剪切点。也可设 S01 `sec: 8`，用紧接的 S02 `audio_in: 4` 指定视觉剪切点。导出的 Hypit 工程把同一音轨作为跨镜音频窗口，并把所需素材放进工程的 `assets/`。
- BGM 使用 180ms 前视、800ms 释放的对白压低。导出 MP4 去除输入容器元数据与 chapters；`--matrix-seed` 可复现轻微缩放和色彩差异，参数写进 `assemble-check.json`。它不替代矩阵查重。`audit-matrix` 写 `MATRIX-AUDIT.json` / `.md`，对重复 BGM、商品基准图及相近口播/开场报错；品牌共用 BGM 可用 `{"allowed_bgm_sha256":["<64 位哈希>"]}` 豁免。
- 超分前通过第 6 步并由用户执行 `allow-superres`。配置 `SUPERRES_BASE_URL` / `SUPERRES_API_KEY`（也接受 `H3_SUPERRES_URL` / `H3_SUPERRES_API_KEY`，密钥可用 macOS Keychain）。`superres --resume` 复用 `qa/superres.task.json` 的任务 ID 和服务器上传偏移；下载校验字节数、SHA-256、尺寸和完整解码后才发布成片。超分结果没有音轨时，把已验收成片的音轨拷回去。完成后用 `advance` 验收第 7 步。

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
