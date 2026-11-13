"""flow.html: a self-contained, offline, draggable workflow diagram of a template (for people, not agents).

Layout follows the content desk's workflow board: inputs (product image, slots) -> per-unit first frame
(image-to-image) -> per-unit video (image-to-video) -> assemble -> output. Frames are referenced relatively
(frames/...), so the file works when opened from the unpacked handoff folder."""

from __future__ import annotations

import html
import json
import re

from desklib import recipe as rcp

COL = 250
ROW = 250
PAD_X, PAD_Y = 24, 56
NODE_W = 210


def _product_labels(tpl: dict, frame: dict) -> list[str]:
    if not frame.get("withProduct"):
        return []
    count, roles = rcp.product_ref_info(tpl)
    return [f"参考图 {i + 2}：产品（{roles[i]}）" for i in range(count)]


def _uses(prompt: str, slot: dict, slots: list[dict]) -> bool:
    if re.search(rf"【\s*(?:{re.escape(slot['key'])}|{re.escape(slot['title'])})\s*】", prompt or ""):
        return True
    return len(slots) == 1 and "【】" in (prompt or "")


def graph(tpl: dict) -> dict:
    rep = tpl.get("replica") or {}
    units = rep.get("units") or []
    slots = tpl.get("slots") or []
    steps = {s.get("id"): s for s in tpl.get("steps") or []}
    nodes, edges = [], []
    y = PAD_Y
    has_inputs = bool(tpl.get("needImage") or slots)
    if tpl.get("needImage"):
        nodes.append({"id": "in-image", "kind": "image", "title": "产品图", "x": PAD_X, "y": y,
                      "body": "执行时由用户提供（白底或干净背景的产品照）"})
        y += 150
    for slot in slots:
        nodes.append({"id": f"in-slot-{slot['key']}", "kind": "slot", "title": slot["title"], "x": PAD_X, "y": y,
                      "body": slot.get("default") or "（无默认值）", "meta": f"槽位 {slot['key']}"})
        y += 110
    col0 = 1 if has_inputs else 0
    videos = []
    for row, unit in enumerate(units):
        top = PAD_Y + row * ROW
        k = unit["index"]
        frame, video = steps.get(f"u{k}-frame"), steps.get(f"u{k}-video")
        if frame:
            nodes.append({"id": frame["id"], "kind": "i2i", "title": re.sub(r"^\d+·", f"段{k} · ", frame.get("title") or ""),
                          "x": PAD_X + COL * col0, "y": top, "image": frame.get("refUrl") or unit.get("refUrl") or "",
                          "meta": f"原片 {unit['start']}–{unit['end']}s · {tpl.get('aspectRatio')}",
                          "prompt": frame.get("prompt") or "",
                          "refs": ["参考图 1：原片首帧（构图 / 机位 / 景别 / 光线）"] + _product_labels(tpl, frame)})
            if frame.get("withProduct") and tpl.get("needImage"):
                edges.append({"from": "in-image", "to": frame["id"], "tone": "image"})
            edges += [{"from": f"in-slot-{s['key']}", "to": frame["id"], "tone": "slot"} for s in slots if _uses(frame.get("prompt"), s, slots)]
        if video:
            shots = len(unit.get("shots") or [])
            name = re.sub(r"^\d+·", "", (frame or {}).get("title") or "").replace(" 首帧", "").strip()
            nodes.append({"id": video["id"], "kind": "i2v", "title": f"段{k} · {name} 视频".replace("  ", " "),
                          "x": PAD_X + COL * (col0 + 1), "y": top,
                          "meta": f"生成 {video.get('duration')}s → 剪到 {video.get('targetDuration')}s" + (f" · {shots} 镜头" if shots > 1 else ""),
                          "line": video.get("line") or "", "prompt": video.get("prompt") or "",
                          "refs": ["首帧：上一步生成的图片"]})
            if frame:
                edges.append({"from": frame["id"], "to": video["id"], "tone": "feed"})
            edges += [{"from": f"in-slot-{s['key']}", "to": video["id"], "tone": "slot"} for s in slots if _uses(video.get("prompt"), s, slots)]
            videos.append(video["id"])
    mid = PAD_Y + max(0, (len(units) * ROW - ROW) // 2)
    total = rep.get("duration")
    nodes.append({"id": "assemble", "kind": "assemble", "title": "按原片时间线拼接", "x": PAD_X + COL * (col0 + 2), "y": mid,
                  "body": f"每段变速 0.8–3 倍塞回原片时长，铺满裁切到 {rep.get('width')}×{rep.get('height')}，按段号顺序拼接；不用原片音轨。"})
    edges += [{"from": v, "to": "assemble", "tone": "feed"} for v in videos]
    nodes.append({"id": "out", "kind": "out", "title": "成片", "x": PAD_X + COL * (col0 + 3), "y": mid,
                  "body": f"{total} 秒 · {rep.get('width')}×{rep.get('height')} · {tpl.get('aspectRatio')}"})
    edges.append({"from": "assemble", "to": "out", "tone": "out"})
    columns = ([{"x": PAD_X, "label": "输入"}] if has_inputs else []) + [
        {"x": PAD_X + COL * col0, "label": "首帧 · 图生图"}, {"x": PAD_X + COL * (col0 + 1), "label": "视频 · 图生视频"},
        {"x": PAD_X + COL * (col0 + 2), "label": "拼接"}, {"x": PAD_X + COL * (col0 + 3), "label": "成片"}]
    return {"nodes": nodes, "edges": edges, "columns": columns}


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · 复刻流程</title>
<style>
:root{--bg:#f6f8fb;--surface:#fff;--line:#dfe4ec;--ink:#111827;--muted:#64748b;--chip:#eef2f7;--dot:rgba(15,23,42,.08);
--image:#06b6d4;--slot:#d97706;--i2i:#4f46e5;--i2v:#7c3aed;--assemble:#0f766e;--out:#168a46;--shadow:0 1px 2px rgba(15,23,42,.06),0 6px 18px rgba(15,23,42,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1218;--surface:#171b23;--line:#2a313d;--ink:#e6e9ef;--muted:#94a0b4;--chip:#222833;--dot:rgba(255,255,255,.06);--shadow:0 1px 2px rgba(0,0,0,.4)}}
:root[data-theme=dark]{--bg:#0f1218;--surface:#171b23;--line:#2a313d;--ink:#e6e9ef;--muted:#94a0b4;--chip:#222833;--dot:rgba(255,255,255,.06);--shadow:0 1px 2px rgba(0,0,0,.4)}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--ink);font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
body{display:flex;flex-direction:column}
header{flex:none;display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--surface)}
header h1{margin:0;font-size:15px}
header p{margin:0;color:var(--muted);font-size:12px;flex-basis:100%}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:11.5px;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:10px;margin-left:auto;font-size:11.5px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.tools{display:flex;gap:6px}
.tools button{font:inherit;font-size:12px;padding:3px 9px;border-radius:7px;border:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer}
.tools button:hover{background:var(--chip)}
main{position:relative;flex:1;display:flex;min-height:0}
#stage{position:relative;flex:1;overflow:hidden;cursor:grab;background-image:radial-gradient(var(--dot) 1px,transparent 1px);background-size:18px 18px;touch-action:none}
#stage.panning{cursor:grabbing}
#world{position:absolute;left:0;top:0;transform-origin:0 0}
svg.wires{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.wire{fill:none;stroke-width:2;opacity:.75}
.wire.image{stroke:var(--image)}.wire.slot{stroke:var(--slot);stroke-dasharray:5 4}.wire.feed{stroke:var(--i2v)}.wire.out{stroke:var(--out)}
.col{position:absolute;top:18px;font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--muted);white-space:nowrap}
.node{position:absolute;width:210px;border:1px solid var(--line);border-top:3px solid var(--tone);border-radius:10px;background:var(--surface);box-shadow:var(--shadow);cursor:grab;user-select:none;transition:box-shadow .15s}
.node:hover{box-shadow:0 8px 24px rgba(15,23,42,.14)}
.node.sel{outline:2px solid var(--tone);outline-offset:1px}
.node.dragging{cursor:grabbing;opacity:.92}
.node.image{--tone:var(--image)}.node.slot{--tone:var(--slot)}.node.i2i{--tone:var(--i2i)}.node.i2v{--tone:var(--i2v)}.node.assemble{--tone:var(--assemble)}.node.out{--tone:var(--out)}
.nh{display:flex;align-items:center;gap:6px;padding:7px 9px 5px}
.nt{font-weight:600;font-size:12.5px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:10px;padding:0 6px;border-radius:999px;color:#fff;background:var(--tone);white-space:nowrap}
.nb{padding:0 9px 9px;display:flex;flex-direction:column;gap:6px}
.nb img{width:100%;height:120px;object-fit:cover;border-radius:7px;background:var(--chip);display:block;pointer-events:none}
.meta{font-size:11px;color:var(--muted)}
.txt{font-size:11.5px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.line{font-size:11.5px;padding:4px 7px;border-radius:6px;background:var(--chip)}
mark{background:color-mix(in srgb,var(--slot) 22%,transparent);color:inherit;border-radius:3px;padding:0 2px}
aside{width:340px;max-width:45%;border-left:1px solid var(--line);background:var(--surface);overflow:auto;padding:14px 16px;display:none}
aside.open{display:block}
aside h2{margin:0 0 4px;font-size:14px}
aside h3{margin:14px 0 4px;font-size:11.5px;color:var(--muted);font-weight:600}
aside pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);padding:8px 10px;border-radius:8px}
aside ul{margin:0;padding-left:18px}
aside img{width:100%;border-radius:8px;margin-top:6px}
aside .close{float:right;border:0;background:none;color:var(--muted);font-size:18px;cursor:pointer}
.hint{position:absolute;left:12px;bottom:10px;font-size:11px;color:var(--muted);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:3px 8px;pointer-events:none}
@media (max-width:700px){aside{position:absolute;right:0;top:0;bottom:0;width:100%;max-width:100%;z-index:5}.legend{margin-left:0}}
@media (prefers-reduced-motion:reduce){.node{transition:none}}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="chips">__CHIPS__</div>
  <div class="legend"><span><i style="background:var(--image)"></i>产品图</span><span><i style="background:var(--slot)"></i>槽位</span><span><i style="background:var(--i2i)"></i>首帧·图生图</span><span><i style="background:var(--i2v)"></i>视频·图生视频</span><span><i style="background:var(--assemble)"></i>拼接</span><span><i style="background:var(--out)"></i>成片</span></div>
  <div class="tools"><button id="zin" title="放大">＋</button><button id="zout" title="缩小">－</button><button id="fit">适配窗口</button><button id="reset">复位布局</button></div>
  <p>__DESC__</p>
</header>
<main>
  <div id="stage"><div id="world"><svg class="wires" id="wires"></svg></div><div class="hint">拖动节点调整布局 · 拖动空白处平移 · 滚轮缩放 · 点节点看完整提示词</div></div>
  <aside id="detail"></aside>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("data").textContent);
  var KEY = "replica-flow:" + data.id;
  var stage = document.getElementById("stage"), world = document.getElementById("world"), svg = document.getElementById("wires");
  var detail = document.getElementById("detail");
  var view = { x: 0, y: 0, k: 1 };
  var pos = {};
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { saved = null; }
  var TAG = { image: "输入", slot: "槽位", i2i: "图生图", i2v: "图生视频", assemble: "拼接", out: "输出" };

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function marked(s) { return esc(s).replace(/【([^】]*)】/g, "<mark>【$1】</mark>"); }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(pos)); } catch (e) {} }

  data.columns.forEach(function (c) {
    var el = document.createElement("div"); el.className = "col"; el.style.left = c.x + "px"; el.textContent = c.label; world.appendChild(el);
  });
  var els = {};
  data.nodes.forEach(function (n) {
    var el = document.createElement("div");
    el.className = "node " + n.kind; el.dataset.id = n.id;
    var body = "";
    if (n.image) body += '<img alt="" src="' + esc(n.image) + '" onerror="this.style.display=\'none\'">';
    if (n.meta) body += '<div class="meta">' + esc(n.meta) + "</div>";
    if (n.body) body += '<div class="txt">' + marked(n.body) + "</div>";
    if (n.line) body += '<div class="line">台词：' + esc(n.line) + "</div>";
    if (n.prompt) body += '<div class="txt">' + marked(n.kind === "i2i" ? n.prompt.split("\n").slice(1).join(" ") : n.prompt.split("\n")[0]) + "</div>";
    el.innerHTML = '<div class="nh"><span class="nt">' + esc(n.title) + '</span><span class="tag">' + TAG[n.kind] + '</span></div><div class="nb">' + body + "</div>";
    world.appendChild(el);
    els[n.id] = el;
  });

  function place(id) { var p = pos[id]; els[id].style.left = p.x + "px"; els[id].style.top = p.y + "px"; }
  function layout(fromSaved) {
    data.nodes.forEach(function (n) {
      var s = fromSaved && saved && saved[n.id];
      pos[n.id] = s && isFinite(s.x) && isFinite(s.y) ? { x: s.x, y: s.y } : { x: n.x, y: n.y };
      place(n.id);
    });
  }
  function wires() {
    var maxX = 0, maxY = 0, out = "";
    data.nodes.forEach(function (n) { var e = els[n.id]; maxX = Math.max(maxX, pos[n.id].x + e.offsetWidth); maxY = Math.max(maxY, pos[n.id].y + e.offsetHeight); });
    data.edges.forEach(function (w) {
      var a = els[w.from], b = els[w.to]; if (!a || !b) return;
      var x1 = pos[w.from].x + a.offsetWidth, y1 = pos[w.from].y + 18, x2 = pos[w.to].x, y2 = pos[w.to].y + 18;
      var dx = Math.max(40, Math.abs(x2 - x1) * 0.5), dir = x2 >= x1 ? 1 : -1;
      out += '<path class="wire ' + w.tone + '" d="M ' + x1 + " " + y1 + " C " + (x1 + dx * dir) + " " + y1 + ", " + (x2 - dx * dir) + " " + y2 + ", " + x2 + " " + y2 + '"/>';
    });
    svg.setAttribute("width", maxX + 80); svg.setAttribute("height", maxY + 80);
    svg.innerHTML = out;
  }
  function apply() { world.style.transform = "translate(" + view.x + "px," + view.y + "px) scale(" + view.k + ")"; }
  function fitWidth() {
    var maxX = 0;
    data.nodes.forEach(function (n) { maxX = Math.max(maxX, pos[n.id].x + els[n.id].offsetWidth); });
    var k = Math.min(1, Math.max(0.6, (stage.clientWidth - 40) / (maxX + 20)));
    view = { k: k, x: Math.max(10, (stage.clientWidth - maxX * k) / 2), y: 10 };
    apply();
  }
  function fit() {
    var maxX = 0, maxY = 0;
    data.nodes.forEach(function (n) { maxX = Math.max(maxX, pos[n.id].x + els[n.id].offsetWidth); maxY = Math.max(maxY, pos[n.id].y + els[n.id].offsetHeight); });
    var k = Math.min(1.2, Math.max(0.3, Math.min((stage.clientWidth - 40) / (maxX + 20), (stage.clientHeight - 40) / (maxY + 20))));
    view = { k: k, x: Math.max(10, (stage.clientWidth - maxX * k) / 2), y: 10 };
    apply();
  }
  function zoom(f, cx, cy) {
    var k = Math.min(2.5, Math.max(0.25, view.k * f));
    cx = cx == null ? stage.clientWidth / 2 : cx; cy = cy == null ? stage.clientHeight / 2 : cy;
    view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k); view.k = k; apply();
  }

  function show(id) {
    var n = data.nodes.find(function (x) { return x.id === id; });
    Object.keys(els).forEach(function (k) { els[k].classList.toggle("sel", k === id); });
    var h = '<button class="close" aria-label="关闭">×</button><h2>' + esc(n.title) + '</h2><div class="meta">' + TAG[n.kind] + (n.meta ? " · " + esc(n.meta) : "") + "</div>";
    if (n.image) h += '<img alt="原片首帧" src="' + esc(n.image) + '">';
    if (n.refs) h += "<h3>输入</h3><ul>" + n.refs.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>";
    if (n.body) h += "<h3>说明</h3><pre>" + marked(n.body) + "</pre>";
    if (n.line) h += "<h3>台词</h3><pre>" + esc(n.line) + "</pre>";
    if (n.prompt) h += "<h3>提示词（【】是要填的槽位）</h3><pre>" + marked(n.prompt) + "</pre>";
    detail.innerHTML = h; detail.classList.add("open");
    detail.querySelector(".close").onclick = function () { detail.classList.remove("open"); Object.keys(els).forEach(function (k) { els[k].classList.remove("sel"); }); };
  }

  var drag = null;
  stage.addEventListener("pointerdown", function (e) {
    var node = e.target.closest(".node");
    drag = node ? { id: node.dataset.id, sx: e.clientX, sy: e.clientY, ox: pos[node.dataset.id].x, oy: pos[node.dataset.id].y, moved: false }
                : { pan: true, sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y, moved: false };
    if (node) node.classList.add("dragging"); else stage.classList.add("panning");
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.pan) { view.x = drag.ox + dx; view.y = drag.oy + dy; apply(); return; }
    pos[drag.id] = { x: Math.round(drag.ox + dx / view.k), y: Math.round(drag.oy + dy / view.k) };
    place(drag.id); wires();
  });
  function end() {
    if (!drag) return;
    if (drag.id) { els[drag.id].classList.remove("dragging"); if (drag.moved) save(); else show(drag.id); }
    stage.classList.remove("panning"); drag = null;
  }
  stage.addEventListener("pointerup", end); stage.addEventListener("pointercancel", end);
  stage.addEventListener("wheel", function (e) { e.preventDefault(); var r = stage.getBoundingClientRect(); zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  document.getElementById("zin").onclick = function () { zoom(1.2); };
  document.getElementById("zout").onclick = function () { zoom(1 / 1.2); };
  document.getElementById("fit").onclick = fit;
  document.getElementById("reset").onclick = function () { try { localStorage.removeItem(KEY); } catch (e) {} saved = null; layout(false); wires(); fitWidth(); };

  layout(true);
  var imgs = world.querySelectorAll("img"), pending = imgs.length;
  function ready() { wires(); fitWidth(); }
  if (!pending) ready();
  imgs.forEach(function (img) { if (img.complete) { if (--pending === 0) ready(); } else { img.addEventListener("load", function () { wires(); }); img.addEventListener("error", function () { wires(); }); } });
  if (pending) ready();
  window.addEventListener("resize", function () { wires(); });
})();
</script>
</body>
</html>
"""


def render(tpl: dict) -> str:
    rep = tpl.get("replica") or {}
    g = graph(tpl)
    payload = {"id": tpl.get("id"), **g}
    chips = [f"{len(rep.get('units') or [])} 段", f"{rep.get('duration')} 秒", f"{rep.get('width')}×{rep.get('height')}",
             tpl.get("aspectRatio") or "", "需要产品图" if tpl.get("needImage") else "不需要产品图",
             f"{len(tpl.get('slots') or [])} 个槽位"]
    if rep.get("voice"):
        chips.append(f"配音：{rep['voice']}")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__TITLE__", html.escape(tpl.get("name") or "复刻模板"))
            .replace("__DESC__", html.escape(tpl.get("desc") or ""))
            .replace("__CHIPS__", "".join(f'<span class="chip">{html.escape(c)}</span>' for c in chips if c))
            .replace("__DATA__", data))
