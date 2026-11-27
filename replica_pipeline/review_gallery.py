"""Interactive HTML Review & Timestamped Comments Gallery (inspired by Hypit Studio).

Generates a self-contained HTML review studio (`qa/review.html` for a single film
or `review.html` for a batch) featuring:
- Video player with clickable shot boundaries + ASR subtitle cue jumps.
- Still comparison board (`qa/still-contact-sheet.jpg`) and 4-frame video contact sheets.
- Lock status pills (SIZE / PERSON / PRODUCT / VLM / H3-VISUAL / VOICE / ASSEMBLE) + candidates.
- Timestamped Comments drawer compatible with Hypit's `FEEDBACK.json` schema.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Any


def _safe_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _rel(target: Path, base_dir: Path) -> str:
    return Path(os.path.relpath(target.resolve(), base_dir.resolve())).as_posix()


def collect_film_review(film: Path, *, out_dir: Path) -> dict[str, Any]:
    """Gather media paths, lock verdicts, shots, cues, and candidates for one film."""
    film = Path(film)
    pip = _safe_json(film / "qa" / "PIPELINE.json") or {}
    plan = _safe_json(film / "qa" / "REBEAT-PLAN.json") or {}
    size_lock = _safe_json(film / "qa" / "SIZE-LOCK.json") or {}
    person_lock = _safe_json(film / "qa" / "PERSON-LOCK.json") or {}
    prod_lock = _safe_json(film / "qa" / "STILL-PRODUCT-LOCK.json") or {}
    vlm_still = _safe_json(film / "qa" / "VLM-STILL-REVIEW.json") or {}
    vis_lock = _safe_json(film / "qa" / "H3-VISUAL-LOCK.json") or {}
    voice_lock = _safe_json(film / "qa" / "VOICE-LOCK.json") or {}
    asm_chk = _safe_json(film / "qa" / "assemble-check.json") or {}
    cues = _safe_json(film / "qa" / "captions.asr-cues.json") or []
    cand_doc = _safe_json(film / "qa" / "CANDIDATES.json") or {}
    feedback_doc = _safe_json(film / "qa" / "FEEDBACK.json") or {}

    video_path = None
    for cand_rel in ("qa/captioned.mp4", "qa/strict-with-bgm.mp4", "final.mp4"):
        p = film / cand_rel
        if p.is_file():
            video_path = _rel(p, out_dir)
            break

    still_board = film / "qa" / "still-contact-sheet.jpg"
    shots_info: list[dict[str, Any]] = []
    for s in plan.get("shots") or []:
        sid = s["id"]
        sheet = film / "qa" / "h3-contact-sheets" / f"{sid}.jpg"
        shot_mp4 = film / "shots" / f"{sid}.mp4"
        cand_items = ((cand_doc.get("shots") or {}).get(sid) or {}).get("items") or []
        shots_info.append(
            {
                "id": sid,
                "sec": s.get("sec"),
                "source": s.get("source", ""),
                "video": _rel(shot_mp4, out_dir) if shot_mp4.is_file() else None,
                "contact_sheet": _rel(sheet, out_dir) if sheet.is_file() else None,
                "visual_status": ((vis_lock.get("shots") or {}).get(sid) or {}).get("status", "PENDING"),
                "vlm_still": ((vlm_still.get("shots") or {}).get(sid) or {}).get("verdict", "PENDING"),
                "candidates_count": len(cand_items),
            }
        )

    def _stage_status(stage: str) -> str:
        return ((pip.get("stages") or {}).get(stage) or {}).get("status", "pending").upper()

    return {
        "film": film.name,
        "current": pip.get("current", "1_read_source"),
        "stages": {k: _stage_status(k) for k in (pip.get("stages") or {})},
        "locks": {
            "size": size_lock.get("source", "MISSING"),
            "person": "OK" if person_lock.get("shots") else "MISSING",
            "product": "OK" if prod_lock.get("shots") else "MISSING",
            "vlm_still": "OK" if vlm_still.get("shots") else "MISSING",
            "h3_visual": "OK" if vis_lock.get("shots") else "MISSING",
            "voice": "OK" if voice_lock else "MISSING",
            "assemble": asm_chk.get("caption_align", "MISSING"),
        },
        "video": video_path,
        "still_board": _rel(still_board, out_dir) if still_board.is_file() else None,
        "shots": shots_info,
        "cues": cues if isinstance(cues, list) else [],
        "feedback": feedback_doc.get("comments") if isinstance(feedback_doc, dict) else [],
        "assemble": asm_chk,
    }


def render_review_html(films_data: list[dict[str, Any]], *, title: str = "Replica Review Studio") -> str:
    """Render a self-contained Hypit Studio #comments-inspired HTML review page."""
    payload_json = json.dumps(films_data, ensure_ascii=False).replace("</", "<\\/")
    escaped_title = html.escape(title)
    generated_at = time.strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    :root {{
      --bg: #0f1116; --panel: #171a22; --panel-2: #1f2430; --border: #2a3040;
      --text: #eef1f7; --muted: #98a2b3; --accent: #7c5cff; --ok: #22c55e;
      --warn: #f59e0b; --fail: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Inter", sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }}
    header {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 14px 22px; border-bottom: 1px solid var(--border); background: var(--panel);
      position: sticky; top: 0; z-index: 20;
    }}
    header h1 {{ margin: 0; font-size: 17px; font-weight: 600; }}
    header .meta {{ color: var(--muted); font-size: 12px; }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; }}
    button, .pill {{
      background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 6px 11px; font-size: 12px; cursor: pointer;
    }}
    button:hover {{ border-color: var(--accent); }}
    button.primary {{ background: var(--accent); border-color: var(--accent); font-weight: 600; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 18px 22px 64px; }}
    .film-tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
    .film-tab.active {{ background: var(--accent); border-color: var(--accent); font-weight: 600; }}
    .grid {{ display: grid; grid-template-columns: 380px 1fr 340px; gap: 18px; align-items: start; }}
    @media (max-width: 1120px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px;
    }}
    .card h2 {{ margin: 0 0 10px; font-size: 14px; color: #d0d7e6; }}
    video {{ width: 100%; border-radius: 10px; background: #000; max-height: 640px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
    .badge {{ font-size: 11px; padding: 3px 8px; border-radius: 999px; border: 1px solid var(--border); }}
    .badge.ok {{ color: #86efac; border-color: rgba(34,197,94,.4); background: rgba(34,197,94,.1); }}
    .badge.warn {{ color: #fde68a; border-color: rgba(245,158,11,.4); background: rgba(245,158,11,.1); }}
    .badge.fail {{ color: #fca5a5; border-color: rgba(239,68,68,.4); background: rgba(239,68,68,.1); }}
    .cues {{ max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }}
    .cue-btn {{
      display: flex; gap: 8px; text-align: left; width: 100%; font-size: 12px;
      background: var(--panel-2); padding: 6px 8px; border-radius: 6px;
    }}
    .cue-time {{ color: #a78bfa; font-variant-numeric: tabular-nums; min-width: 48px; }}
    .board-img {{ width: 100%; border-radius: 8px; border: 1px solid var(--border); }}
    .shots-list {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 10px; margin-top: 10px; }}
    .shot-item {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 8px; }}
    .shot-item img {{ width: 100%; border-radius: 6px; margin-top: 6px; }}
    .comment-form {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }}
    textarea {{
      width: 100%; min-height: 70px; background: var(--bg); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px; padding: 8px; font-size: 12px;
    }}
    .comment-list {{ display: flex; flex-direction: column; gap: 8px; max-height: 520px; overflow-y: auto; }}
    .comment-item {{
      background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
    }}
    .comment-item.resolved {{ opacity: .55; }}
    .comment-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>{escaped_title}</h1>
    <div class="meta">Generated {generated_at} · Timestamped Review &amp; QA Gallery</div>
  </div>
  <div class="toolbar">
    <button class="primary" onclick="exportFeedback()">导出 FEEDBACK.json</button>
  </div>
</header>
<main>
  <div id="filmTabs" class="film-tabs"></div>
  <div id="workspace" class="grid"></div>
</main>
<script>
const FILMS = {payload_json};
const STORE_KEY = "replica_review_feedback_v1";
let activeFilmIdx = 0;

function esc(s) {{
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}}
function loadStoredComments() {{
  try {{ return JSON.parse(localStorage.getItem(STORE_KEY) || "{{}}"); }} catch {{ return {{}}; }}
}}
function saveStoredComments(map) {{
  localStorage.setItem(STORE_KEY, JSON.stringify(map));
}}
function getFilmComments(filmName, seed) {{
  const map = loadStoredComments();
  if (!map[filmName]) {{
    map[filmName] = Array.isArray(seed) ? seed : [];
    saveStoredComments(map);
  }}
  return map[filmName];
}}
function statusClass(val) {{
  const u = String(val || "").toUpperCase();
  if (u === "PASS" || u === "OK" || u === "ASR_WORDS" || u === "USER_SPEC" || u === "FIRST_FRAME") return "ok";
  if (u === "FAIL" || u === "MISSING") return "fail";
  return "warn";
}}
function seekTo(sec) {{
  const v = document.getElementById("player");
  if (!v) return;
  v.currentTime = Number(sec || 0);
  v.play().catch(() => {{}});
}}
function addComment(filmName) {{
  const v = document.getElementById("player");
  const ta = document.getElementById("commentInput");
  const text = (ta.value || "").trim();
  if (!text) return;
  const at = v ? Math.round(v.currentTime * 100) / 100 : 0;
  const map = loadStoredComments();
  const list = map[filmName] || [];
  list.push({{ id: "c-" + Date.now(), film: filmName, at, text, resolved: false }});
  list.sort((a, b) => a.at - b.at);
  map[filmName] = list;
  saveStoredComments(map);
  ta.value = "";
  render();
}}
function addCommentActive() {{
  const f = FILMS[activeFilmIdx];
  if (!f) return;
  addComment(f.film);
}}
function toggleResolvedByBtn(btn) {{
  const f = FILMS[activeFilmIdx];
  if (!f) return;
  toggleResolved(f.film, btn.getAttribute("data-cid"));
}}
function toggleResolved(filmName, cid) {{
  const map = loadStoredComments();
  const list = map[filmName] || [];
  const item = list.find(x => x.id === cid);
  if (item) item.resolved = !item.resolved;
  saveStoredComments(map);
  render();
}}
function exportFeedback() {{
  const f = FILMS[activeFilmIdx];
  const comments = getFilmComments(f.film, f.feedback);
  const blob = new Blob([JSON.stringify({{ film: f.film, comments }}, null, 2) + "\\n"], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "FEEDBACK.json";
  a.click();
}}
function render() {{
  const tabs = document.getElementById("filmTabs");
  tabs.innerHTML = FILMS.map((f, i) =>
    `<button class="film-tab ${{i===activeFilmIdx?'active':''}}" onclick="activeFilmIdx=${{i}};render()">${{esc(f.film)}} (${{esc(f.current)}})</button>`
  ).join("");
  const f = FILMS[activeFilmIdx];
  if (!f) return;
  const comments = getFilmComments(f.film, f.feedback);
  const lockBadges = Object.entries(f.locks).map(([k, v]) =>
    `<span class="badge ${{statusClass(v)}}">${{esc(k)}}: ${{esc(v)}}</span>`
  ).join("");
  const cuesHtml = (f.cues || []).map(c =>
    `<button class="cue-btn" onclick="seekTo(${{Number(c.start) || 0}})"><span class="cue-time">${{Number(c.start).toFixed(2)}}s</span><span>${{esc(c.text)}}</span></button>`
  ).join("") || `<div class="meta">暂无 captions.asr-cues.json</div>`;
  const shotsHtml = (f.shots || []).map(s => `
    <div class="shot-item">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>${{esc(s.id)}} (${{esc(s.sec ?? '?')}}s)</strong>
        <span class="badge ${{statusClass(s.visual_status)}}">VLM:${{esc(s.vlm_still)}} / H3:${{esc(s.visual_status)}}</span>
      </div>
      <div class="meta">candidates: ${{Number(s.candidates_count) || 0}} · ${{esc(s.source)}}</div>
      ${{s.contact_sheet ? `<img src="${{esc(s.contact_sheet)}}" alt="${{esc(s.id)}}" />` : ""}}
    </div>
  `).join("");
  const commentsHtml = comments.map(c => `
    <div class="comment-item ${{c.resolved ? 'resolved' : ''}}">
      <div class="comment-head">
        <button onclick="seekTo(${{Number(c.at) || 0}})">@${{Number(c.at).toFixed(2)}}s</button>
        <button data-cid="${{esc(c.id)}}" onclick="toggleResolvedByBtn(this)">${{c.resolved ? '已解决' : '标记完成'}}</button>
      </div>
      <div>${{esc(c.text)}}</div>
    </div>
  `).join("") || `<div class="meta">播放并在任意秒数添加时间戳意见</div>`;

  document.getElementById("workspace").innerHTML = `
    <section class="card">
      <h2>成片回放 &amp; ASR 字幕跳转</h2>
      ${{f.video ? `<video id="player" controls playsinline src="${{esc(f.video)}}"></video>` : `<div class="meta">未找到成片视频</div>`}}
      <div class="cues">${{cuesHtml}}</div>
    </section>
    <section class="card">
      <h2>门控锁 &amp; 同框对比</h2>
      <div class="badges">${{lockBadges}}</div>
      ${{f.still_board ? `<img class="board-img" src="${{esc(f.still_board)}}" alt="still-contact-sheet" />` : `<div class="meta">未生成 still-contact-sheet.jpg</div>`}}
      <div class="shots-list">${{shotsHtml}}</div>
    </section>
    <aside class="card">
      <h2>Comments (#comments)</h2>
      <div class="comment-form">
        <textarea id="commentInput" placeholder="在当前播放秒数记录修改意见，如：S02 商品略偏大 / 此处字幕延迟"></textarea>
        <button class="primary" onclick="addCommentActive()">在当前秒数添加评论</button>
      </div>
      <div class="comment-list">${{commentsHtml}}</div>
    </aside>
  `;
}}
render();
</script>
</body>
</html>
"""


def write_review_html(
    *,
    film: Path | None = None,
    batch: Path | None = None,
    out: Path | None = None,
) -> Path:
    """Build single-film (`qa/review.html`) or batch (`review.html`) interactive gallery."""
    if film is None and batch is None:
        raise ValueError("Provide either film or batch directory")
    if film is not None:
        film = Path(film)
        out_path = Path(out) if out else (film / "qa" / "review.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        data = [collect_film_review(film, out_dir=out_path.parent)]
        title = f"Review — {film.name}"
    else:
        batch = Path(batch)  # type: ignore[arg-type]
        out_path = Path(out) if out else (batch / "review.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        films = sorted(p for p in batch.iterdir() if p.is_dir() and p.name[:2].isdigit() and "-" in p.name)
        data = [collect_film_review(p, out_dir=out_path.parent) for p in films]
        title = f"Batch Review — {batch.name}"

    out_path.write_text(render_review_html(data, title=title))
    return out_path
