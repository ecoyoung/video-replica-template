"""Handoff package: a self-contained folder (+ zip) another agent can execute with its own image / video tools.

  <out>/<tpl id>/
    template.json   recipe in content-desk fields, paths relative to the package, plus a machine-readable "handoff" block
    AGENT.md        step-by-step instructions for the executing agent (inputs, slots, line rewrite, per-unit steps, assembly, checks)
    flow.html       draggable workflow diagram for people (offline, single file, reads frames/ relatively)
    frames/         first frame of every unit (composition reference for the image step)
  <out>/<tpl id>.zip
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

from desklib import flowhtml
from desklib import profile as look
from desklib import recipe as rcp

SPEED_MIN, SPEED_MAX = 0.8, 3.0  # play-speed range when fitting a generated clip into the original unit length


def _block(text: str) -> str:
    return "```text\n" + (text or "").strip() + "\n```"


def _unit_steps(tpl: dict) -> list[tuple[dict, dict, dict]]:
    by_unit: dict[int, dict] = {}
    for step in tpl.get("steps") or []:
        if step.get("unit") is not None:
            by_unit.setdefault(int(step["unit"]), {})["video" if step["kind"] == "i2v" else "frame"] = step
    return [(u, by_unit.get(int(u["index"]), {}).get("frame", {}), by_unit.get(int(u["index"]), {}).get("video", {}))
            for u in (tpl.get("replica") or {}).get("units") or []]


def _product_input_line(items: list[dict]) -> str:
    if len(items) <= 1:
        return "**必填**，1 张，白底或干净背景的产品照效果最好"
    listing = "、".join(f"参考图 {i + 2} = {p['label'] or '产品'}" + (f"（{p['where']}）" if p["where"] else "")
                        for i, p in enumerate(items))
    return (f"**必填**，共 {len(items)} 张，是 {len(items)} 件**不同产品**（不是同一件的多个角度）：{listing}；"
            "缺哪件，只用到它的段就先跳过，不要拿别的产品顶替")


def _product_step_lines(tpl: dict, frame: dict) -> list[str]:
    if not frame.get("withProduct"):
        return ["- 参考图 2（产品）：不需要"]
    items = rcp.step_product_list(tpl, frame)
    return [f"- 参考图 {i + 2}：`{p['image']}` → {p['label'] or '产品'}"
            + (f"（{p['where']}）" if p["where"] else "") for i, p in enumerate(items)]


def agent_markdown(tpl: dict) -> str:
    rep = tpl.get("replica") or {}
    slots = tpl.get("slots") or []
    need = bool(tpl.get("needImage"))
    items = products(tpl)
    units = _unit_steps(tpl)
    w, h = rep.get("width"), rep.get("height")
    lines = [
        f"# {tpl['name']} · 复刻模板执行说明",
        "",
        f"{tpl.get('desc') or ''}".strip(),
        "",
        f"- 原片：{rep.get('duration', 0)} 秒，{w}×{h}，画幅 {tpl.get('aspectRatio')}，共 {len(units)} 段（按原片镜头和节奏切好）。",
        "- 每段原片首帧在 `frames/`；流程图见 `flow.html`（给人看的，可拖动）。参考片不随包附带。",
        f"- 配音描述：{rep.get('voice') or '无口播'}" + (f"（语言 {rep.get('language')}）" if rep.get("language") else ""),
        "",
        "**原则**：不要把整条参考片交给视频模型说「换产品」。按下面的段，每段先出一张首帧静帧，再用这张静帧做首帧生成这一段视频，最后按原片时长拼回去。",
        "",
        "## 1. 需要的输入",
        "",
        f"- 产品图：{_product_input_line(items) if need else '不需要（这条片子没有可替换的商品）'}",
    ]
    if slots:
        lines += ["- 可替换内容（不填就用默认值）：", "", "| key | 槽名 | 默认值 |", "|---|---|---|"]
        lines += [f"| {s['key']} | {s['title']} | {s.get('default', '')} |" for s in slots]
    else:
        lines.append("- 没有可替换内容（槽位）。")
    lines += [
        "",
        "## 2. 填槽位",
        "",
        "把所有提示词里的 `【槽名】` 或 `【key】` 换成用户给的值（没给就用默认值）。只有一个槽位时 `【】` 也换成它。",
        "",
        "## 3. 改写台词（槽位改了、且有台词时）",
        "",
        "每段视频提示词里有一行 `口播（…）：「台词」`。用户换了槽位内容时，用文本模型按下面的要求改写全部台词，"
        "再把新台词替换回各段提示词的「」里（视频模型和配音念同一句）。改完检查：台词里不能再出现任何原值。",
        "",
        _block(rcp.rewrite_request(["<第1段台词>", "<第2段台词>", "…"], [{"title": "<槽名>", "old": "<原值>", "new": "<新值>"}], rep.get("language") or "")),
        "",
        "## 4. 逐段生成",
        "",
        "段与段之间互不依赖，可以并行；同一段内先首帧后视频。比例一律用原片画幅，不要让模型自己选。",
        "",
        "| 段 | 原片时间 | 原片时长 | 生成视频时长 | 画面（实测） | 首帧参考图 |",
        "|---|---|---|---|---|---|",
    ]
    for unit, frame, video in units:
        lines.append(f"| {unit['index']} | {unit['start']}–{unit['end']}s | {unit['duration']}s | "
                     f"{video.get('duration', unit.get('generate'))}s | {look.look_text(unit.get('look') or {})} | "
                     f"`{frame.get('refUrl', '')}` |")
    drift = look.warnings([u for u, _f, _v in units])
    if drift:
        lines += ["", "**跨段一致性**（工具量出来的，拼回去之前处理）：", ""] + [f"- {w}" for w in drift]
    for unit, frame, video in units:
        title = re.sub(r"^\d+·", "", str(frame.get("title") or f"第 {unit['index']} 段")).replace(" 首帧", "")
        lines += [
            "",
            f"### 段 {unit['index']} · {title}",
            "",
            f"**步骤 `{frame.get('id')}`：图生图（首帧）**",
            f"- 参考图 1（构图、机位、景别、光线）：`{frame.get('refUrl', '')}`",
            f"- 画面（实测）：{look.look_text(unit.get('look') or {}) or '—'}",
            *_product_step_lines(tpl, frame),
            f"- 比例：{frame.get('aspectRatio')}",
            "- 提示词：",
            "",
            _block(frame.get("prompt", "")),
            "",
            f"**步骤 `{video.get('id')}`：图生视频**",
            f"- 首帧：上一步生成的图片",
            f"- 时长：{video.get('duration')} 秒（之后剪到 {video.get('targetDuration')} 秒）；比例：{video.get('aspectRatio')}",
            "- 提示词：",
            "",
            _block(video.get("prompt", "")),
        ]
    total = round(sum(float(u["duration"]) for u, _, _ in units), 3)
    lines += [
        "",
        "## 5. 拼接",
        "",
        f"按段号顺序把每段视频放回原片时间线，成片总长 {total} 秒、{w}×{h}：",
        "",
        f"1. 每段播放速度 = 生成视频时长 ÷ 该段原片时长，限制在 {SPEED_MIN}–{SPEED_MAX} 倍（模型最短 4 秒，短镜头整段加速比只截前几秒自然）；"
        "声音跟画面同倍速、保持音调。",
        f"2. 缩放并居中裁切到 {w}×{h}（铺满，不留黑边），帧率不超过 30；不够长补最后一帧，超长截掉，正好等于该段原片时长。",
        "3. 顺序拼接。声音默认用视频模型按台词念的人声；有单独配音时，每段配音前留 0.1 秒、超长就加速塞进该段时长。",
        "4. 不要用原片音轨（里面有原片人声和原产品名）。需要背景音乐时另配，音量约 0.35 垫在人声下。",
        "",
        "## 6. 验收",
        "",
        "- 画幅和原片一致；总时长和段数一致。",
        "- 每段首帧：产品外形、颜色、logo 和产品图一致；构图、机位对得上 `frames/` 里的原片首帧；没有残留原产品或原品牌。",
        "- 每段视频：动作和运镜符合提示词，产品不变形、不消失；没有字幕、花字、水印。",
        "- 台词：没有残留原值，每段说得完。",
        "- 不满意只重做那一段（先首帧再视频），其他段不动。不要声称和原片一比一。",
        "",
        "## 文件",
        "",
        "- `template.json`：模板本体（字段与内容复刻台配方一致：`slots`、`steps`、`replica.units`），`handoff` 是机器可读的执行摘要。",
        "- `frames/u<段号>-first.jpg`：原片每段首帧。",
        "- `flow.html`：流程图（给人看，浏览器打开，可拖动节点、点节点看完整提示词）。",
    ]
    return "\n".join(lines).replace("\n\n\n", "\n\n") + "\n"


def products(tpl: dict) -> list[dict]:
    """The distinct products the package expects, each with a stable image handle."""
    return rcp.product_list(tpl)


def handoff_block(tpl: dict) -> dict:
    rep = tpl.get("replica") or {}
    units = _unit_steps(tpl)
    items = products(tpl)
    return {
        "schema": "video-replica-handoff",
        "version": 2,  # 2: inputs.products (one image per distinct product) replaces the single-image contract
        "instructions": "AGENT.md",
        "diagram": "flow.html",
        "inputs": {
            "productImage": "required" if tpl.get("needImage") else "none",
            **({"products": items} if items else {}),
            "slots": [{"key": s["key"], "title": s["title"], "default": s.get("default", "")} for s in tpl.get("slots") or []],
        },
        "output": {"width": rep.get("width"), "height": rep.get("height"), "aspectRatio": tpl.get("aspectRatio"),
                   "duration": rep.get("duration"), "fpsMax": 30},
        "units": [
            {
                "index": u["index"], "sourceStart": u["start"], "sourceEnd": u["end"], "targetDuration": u["duration"],
                "frame": {"step": f.get("id"), "kind": "image-to-image",
                          "references": [f.get("refUrl")] + [p["image"] for p in rcp.step_product_list(tpl, f)]
                          if f.get("withProduct") else [f.get("refUrl")],
                          "prompt": f.get("prompt"), "aspectRatio": f.get("aspectRatio")},
                "video": {"step": v.get("id"), "kind": "image-to-video", "firstFrame": f"<output of {f.get('id')}>",
                          "duration": v.get("duration"), "prompt": v.get("prompt"), "line": v.get("line", ""), "aspectRatio": v.get("aspectRatio")},
            }
            for u, f, v in units
        ],
        "assembly": {"order": "by unit index", "speedRange": [SPEED_MIN, SPEED_MAX], "fit": "scale-crop-fill", "useReferenceAudio": False,
                     "bgmVolume": 0.35},
        "rules": [
            "slot placeholders 【槽名】 / 【key】 / 【】 must be filled before any generation",
            "rewrite lines when slot values change, then put them back inside 「」 in each video prompt",
            "image step first, then image-to-video with that image as the first frame; never send the whole reference video to a generator",
            "use the template aspect ratio for every step",
        ],
    }


def build_package(wd: Path, recipe: dict, out_root: Path, name: str | None = None, desc: str = "",
                  category=None, units: list[int] | None = None, slot_choices: list[dict] | None = None) -> dict:
    if recipe.get("mode") != "replica":
        raise ValueError("交付包只支持按镜头复刻的模板（整片模式的配方没有分段和首帧）")
    tpl = rcp.build_template(recipe, None, name or recipe.get("name") or "复刻模板", desc, category, slot_choices, units)
    folder = out_root / tpl["id"]  # ASCII only: zip tools and the receiving agent may not handle non-ASCII paths
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "frames").mkdir(parents=True)
    mapping: dict[str, str] = {}
    for item in [*(tpl.get("replica") or {}).get("units", []), *(tpl.get("steps") or [])]:
        ref = item.get("refUrl")
        if ref and ref not in mapping:
            src = wd / ref
            if not src.is_file():
                raise FileNotFoundError(f"首帧不见了：{src}（重新 ingest）")
            dest = Path("frames") / src.name
            shutil.copyfile(src, folder / dest)
            mapping[ref] = dest.as_posix()
        if ref:
            item["refUrl"] = mapping[ref]
    tpl["coverUrl"] = mapping.get(tpl.get("coverUrl") or "", next(iter(mapping.values()), ""))
    tpl["previewM3u8"] = ""  # the reference video is not shipped
    for key in ("sampleUrl", "sourceJobId"):
        tpl.pop(key, None)
    tpl["handoff"] = handoff_block(tpl)
    (folder / "template.json").write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "AGENT.md").write_text(agent_markdown(tpl), encoding="utf-8")
    (folder / "flow.html").write_text(flowhtml.render(tpl), encoding="utf-8")
    archive = shutil.make_archive(str(folder), "zip", root_dir=folder.parent, base_dir=folder.name)
    return {"folder": str(folder), "zip": archive, "template": copy.deepcopy(tpl)}
