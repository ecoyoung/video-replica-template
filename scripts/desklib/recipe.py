"""Recipe (template) building. Prompts and rules follow the content desk (replica.py / vision.py / draft.py /
templates.py / stitch.py). The vision-model step is done by the calling agent: this module writes the request
and turns the agent's JSON answer into a recipe."""

from __future__ import annotations

import copy
import json
import re
import secrets
import time

from desklib import profile as look
from desklib.plan import LANGUAGE_NAMES

KINDS = {"t2i", "i2i", "i2v"}
ASPECTS = {"9:16", "16:9", "1:1", "3:4", "4:3"}


def nid() -> str:
    return secrets.token_hex(6)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- per-unit request (shot mode)

SYSTEM = "你是短视频复刻导演。镜头切分已经由工具测好，你只按段描述画面并写生成提示词。只输出一个 JSON 对象，不要 Markdown。"


def unit_line(unit: dict) -> str:
    shots = unit["shots"]
    text = f"段{unit['index']}：原片 {unit['start']:.1f}–{unit['end']:.1f}s（{unit['duration']:.1f}s）"
    if len(shots) > 1:
        cuts = "、".join(f"{s['start']:.1f}–{s['end']:.1f}s" for s in shots)
        text += f"，含 {len(shots)} 个镜头（段内时间 {cuts}），镜头之间硬切"
    text += f"，将生成 {unit['generate']}s 视频再剪到原时长"
    if unit.get("look"):
        text += f"；工具量出的画面：{look.look_text(unit['look'])}"
    if unit.get("speech"):
        text += f"；原片口播（语音识别）：「{unit['speech']}」"
    return text


def instructions(name: str, units: list[dict], meta: dict) -> str:
    table = "\n".join(unit_line(unit) for unit in units)
    return (
        f"文件名：{name}\n画幅：{meta.get('aspectRatio') or '9:16'}，时长 {float(meta.get('duration') or 0):.1f}s。\n"
        f"工具已把参考片切成 {len(units)} 段（下面每段附了首帧图，顺序与表一致）：\n{table}\n\n"
        "按这个结构输出：\n"
        '{"desc":"一句话说这条片子拍了什么","needImage":true,"aspectRatio":"9:16",'
        '"product":{"name":"片子里展示的产品（品牌+品类）","aliases":["画面或口播里对它的其他叫法"]},'
        '"slots":[{"title":"槽名","default":"参考片里的原文"}],'
        '"voice":"口播的声音：性别、年龄感、语气，如「年轻女声，轻快」（看画面里的人推断，看不出写自然人声）；没有口播给空字符串",'
        f'"units":[{{"title":"不超过 8 个字","keyframe":"首帧静帧提示词","video":"视频提示词","line":"这一段的口播台词"}}]}}\n'
        f"规则：\n"
        f"1. units 必须正好 {len(units)} 个，顺序和上表一致，不要合并或拆分。\n"
        "2. keyframe：照这一段首帧写一张静帧——景别、机位角度、构图、场景、光线、人物和产品的位置。"
        "生成时会把原片首帧当构图参考图一起传给模型。\n"
        "3. video：开头写景别和运镜（推/拉/摇/移/跟/固定/手持），再写动作过程；"
        "段里有多个镜头时按「镜头1（0.0–1.4s）：…；镜头2（1.4–3.2s）：…」逐个写，最后一句写这一段的结束状态。\n"
        "4. 片子在展示一个可以换成别家产品的商品时 needImage 为 true：产品本身只写「上传的产品」，"
        "不要写参考片里产品的颜色、包装和品牌；场景里的颜色照写。没有可替换的商品时 needImage 为 false。\n"
        "5. 这条片子要做成可复用的模板：换产品时通常要跟着换的具体内容抽成 slots，最多 5 个。"
        "优先考虑：食材/配料、道具、场景/背景、人物外观、口播台词。"
        "槽名 2–4 个字（如 食材、道具、场景），default 填参考片里的原值；"
        "所有用到这项内容的提示词都写成【槽名】，不要再写具体值。产品本身不要做成槽位（它由上传的产品图决定）。"
        "没有适合替换的内容就给空数组。\n"
        "6. line：把这一段的「原片口播」改写成这一段的台词——保持原片的语言和意思，长度要能在这一段的时长里说完；"
        "提到产品时用品类说法（如 this blender），不要说参考片的品牌；表里这一段没有原片口播就给空字符串，"
        "不要把画面上的字幕当成口播。\n"
        "7. 提示词里不要字幕、花字、贴纸，也不要写分析过程。\n"
        "8. 表里每段末尾的「工具量出的画面」是逐帧量出来的运镜、明暗、冷暖，不是猜的：video 提示词的运镜照它写，"
        "画面比它还亮/还暗、还暖/还冷时按你看到的写，但运镜不要跟它冲突。"
    )


def request_markdown(name: str, units: list[dict], meta: dict, frames: list[str], video: str) -> str:
    """The file the agent reads before looking at the video: system role, frame files, and the exact instructions."""
    frame_lines = "\n".join(f"- 段{u['index']} 首帧：`{path}`" for u, path in zip(units, frames))
    return (
        "# 拆片请求（按镜头复刻）\n\n"
        f"角色：{SYSTEM}\n\n"
        f"参考片：`{video}`（整条看一遍，留意动作、运镜、口播语气）\n\n"
        f"每段首帧（逐张看，顺序与段表一致）：\n{frame_lines}\n\n"
        "---\n\n"
        f"{instructions(name, units, meta)}\n\n"
        "---\n\n"
        "把 JSON 原样写进 `vlm.json`，再调用 recipe。\n"
    )


def whole_request_markdown(name: str, video: str) -> str:
    """Fallback when shot detection is unavailable: the whole video becomes a chain of at most 6 steps."""
    return (
        "# 拆片请求（整片模式：切镜不可用时的兜底）\n\n"
        "角色：你把参考广告片拆成可复刻的生成配方。只输出一个 JSON 对象，不要 Markdown。\n\n"
        f"参考片：`{video}`\n\n---\n\n"
        f"文件名：{name}\n"
        "按这个结构输出："
        '{"desc":"一句话","needImage":true,"aspectRatio":"9:16","slots":[{"title":"槽名","default":"示例"}],'
        '"shots":[{"kind":"i2i","title":"镜头名","prompt":"给模型的提示词"},'
        '{"kind":"i2v","title":"镜头名","prompt":"给模型的提示词","duration":5}]}\n'
        "规则：shots 1 到 6 步，先静帧后视频，视频步骤后面不能再接静帧。"
        "kind 只能是 t2i、i2i、i2v，并且最后一步必须是 i2v。"
        "观众要换成自己的产品图时 needImage 为 true，静帧用 i2i，不要用 t2i。"
        "没有可替换产品时 needImage 为 false，静帧才可以用 t2i。"
        "可变台词、横幅、痛点放进 slots，并在 prompt 里写成【槽名】。"
        "prompt 写画面、镜头和动作。镜头风格、场景、材质改造照参考片写具体。"
        "需要换成用户产品时，产品本身写「保持上传产品的外形、颜色和 logo」，不要抄参考片上的颜色和包装；场景里的颜色照写。"
        "不要写分析过程，不要字幕。\n\n---\n\n"
        "把 JSON 原样写进 `vlm.json`，再调用 recipe。\n"
    )


# ---------------------------------------------------------------- answer normalisation


def parse_object(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    raw = (fenced.group(1) if fenced else text).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型回答里没有 JSON 对象")
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型回答不是 JSON 对象")
    return value


def _as_shots(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    shots = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        title = str(item.get("title") or "").strip()[:40]
        prompt = str(item.get("prompt") or "").strip()[:2000]
        if not title and not prompt:
            continue
        shot = {"kind": item["kind"], "title": title or "镜头", "prompt": prompt}
        duration = item.get("duration")
        if shot["kind"] == "i2v" and isinstance(duration, (int, float)) and 3 <= duration <= 15:
            shot["duration"] = round(duration)
        shots.append(shot)
        if len(shots) >= 6:
            break
    return shots


def normalize_recipe(value: dict) -> dict:
    """vision.normalize_recipe: shared fields (desc / needImage / aspectRatio / slots) and whole-mode shots."""
    need_image = value.get("needImage") is True
    shots = _as_shots(value.get("shots"))
    if not shots:
        raise ValueError("模型没有拆出镜头")
    if need_image:
        for shot in shots:
            if shot["kind"] == "t2i":
                shot["kind"] = "i2i"
    images = [shot for shot in shots if shot["kind"] != "i2v"]
    videos = [shot for shot in shots if shot["kind"] == "i2v"]
    if not videos:
        last = images[-1] if images else None
        videos.append({"kind": "i2v", "title": "成片", "prompt": (last or {}).get("prompt") or "", "duration": 5})
    slots = []
    for slot in value.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        title = str(slot.get("title") or "").strip()[:24]
        if not title:
            continue
        default = str(slot.get("default") or "").strip()[:80]
        slots.append({"title": title, "default": default} if default else {"title": title})
        if len(slots) >= 5:
            break
    ratio = value.get("aspectRatio")
    aspect = ratio if isinstance(ratio, str) and re.fullmatch(r"\d+:\d+", ratio) else "9:16"
    return {"desc": str(value.get("desc") or "").strip()[:160], "needImage": need_image, "aspectRatio": aspect,
            "slots": slots, "shots": [*images, *videos][:6]}


def normalize_units_answer(value: dict, units: list[dict]) -> dict:
    rows = value.get("units") if isinstance(value.get("units"), list) else []
    if len(rows) != len(units):
        raise ValueError(f"模型返回了 {len(rows)} 段，应为 {len(units)} 段（units 必须和段表一一对应）")
    out_units = []
    for k, row in enumerate(rows, 1):
        row = row if isinstance(row, dict) else {}
        keyframe = str(row.get("keyframe") or "").strip()[:1500]
        video = str(row.get("video") or "").strip()[:2000]
        if not keyframe and not video:
            raise ValueError(f"第 {k} 段没有提示词")
        out_units.append({
            "title": str(row.get("title") or "").strip()[:16] or f"第 {k} 段",
            "keyframe": keyframe or video,
            "video": video or keyframe,
            "line": str(row.get("line") or "").strip()[:400],
        })
    base = normalize_recipe({**value, "shots": [{"kind": "i2v", "title": "x", "prompt": "x"}]})
    product = value.get("product") if isinstance(value.get("product"), dict) else {}
    names = [product.get("name"), *(product.get("aliases") or [])] if product else []
    terms = [str(n).strip()[:40] for n in names if isinstance(n, str) and len(str(n).strip()) >= 2][:8]
    voice = str(value.get("voice") or "").strip()[:60]
    return {**base, "units": out_units, "productTerms": terms, "voice": voice}


PRODUCT_SLOT_TITLES = {"产品", "商品", "产品名", "商品名", "主产品"}
ITEM_SPLIT = r"[、，,；;+/]|和|与|及"


def _is_product_item(item: str, terms: list[str]) -> bool:
    text = item.strip().lower()
    if not text:
        return False
    for term in terms:
        term_l = term.lower()
        if term_l in text or text in term_l:
            return True
        for word in re.findall(r"[a-z][a-z0-9]{2,}", term_l):
            if word in text:
                return True
    return False


def drop_product_slots(seen: dict) -> dict:
    """The product comes from the uploaded product image, so it can never be a slot.

    Dropping is deliberate, but silent drops confuse people, so remember what went: the CLI turns this into a
    warning that says where the product information belongs instead (in the product images).
    """
    terms = seen.get("productTerms") or []
    slots = []
    dropped_slots, dropped_items = [], []
    for slot in seen.get("slots") or []:
        title = str(slot.get("title") or "").strip()
        if title in PRODUCT_SLOT_TITLES:
            dropped_slots.append(title)
            continue
        default = str(slot.get("default") or "")
        items = [x.strip() for x in re.split(ITEM_SPLIT, default) if x.strip()]
        kept = [x for x in items if not _is_product_item(x, terms)]
        if items and not kept:
            dropped_slots.append(title)
            continue
        if len(kept) != len(items):
            dropped_items.append(title)
        slots.append({**slot, "default": "、".join(kept)} if len(kept) != len(items) else slot)
    return {**seen, "slots": slots, "droppedProductSlots": {"slots": dropped_slots, "items": dropped_items}}


def slotify_fallback(seen: dict) -> dict:
    """Replace each item of a slot's original value with 【槽名】 in keyframe / video prompts."""
    units = [dict(u) for u in seen["units"]]
    for slot in seen.get("slots") or []:
        items = [x.strip() for x in re.split(r"[、，,+/和与及]", str(slot.get("default") or "")) if len(x.strip()) >= 2]
        for unit in units:
            for field in ("keyframe", "video"):
                text = unit.get(field)
                if not text:
                    continue
                for item in sorted(items, key=len, reverse=True):
                    text = text.replace(item, f"【{slot['title']}】")
                title = re.escape(slot["title"])
                unit[field] = re.sub(rf"(【{title}】[、，与和及]?)+【{title}】", f"【{slot['title']}】", text)
    return {**seen, "units": units}


SLOTIFY_SYSTEM = "你是提示词编辑。只做替换，不改写、不润色、不增删其他内容。只输出一个 JSON 对象。"


def slotify_request(seen: dict) -> str:
    slots = [s for s in seen.get("slots") or [] if s.get("title")]
    payload = {"slots": [{"槽名": s["title"], "原值": s.get("default") or ""} for s in slots],
               "units": [{"keyframe": u["keyframe"], "video": u["video"]} for u in seen["units"]]}
    return (
        f"角色：{SLOTIFY_SYSTEM}\n\n"
        "下面是一条视频每段的生成提示词，以及要做成可替换槽位的内容。\n"
        "把 keyframe 和 video 里所有指代某个槽位内容的词（包括原值里的每一项、它们的同义说法和具体描述）替换成【槽名】，"
        "同一处出现多项时只写一次【槽名】。不是槽位内容的文字一个字都不要改；产品本身不要替换。\n"
        "输出结构和输入的 units 完全一致：{\"units\":[{\"keyframe\":\"...\",\"video\":\"...\"}]}\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def unslotted(seen: dict) -> list[str]:
    """Slot values still written literally in prompts after slotify (the agent may run slotify_request)."""
    left = []
    for slot in seen.get("slots") or []:
        items = [x.strip() for x in re.split(r"[、，,+/和与及]", str(slot.get("default") or "")) if len(x.strip()) >= 2]
        for unit in seen["units"]:
            text = f"{unit.get('keyframe', '')} {unit.get('video', '')}"
            left += [f"{slot['title']}:{item}" for item in items if item in text]
    return sorted(set(left))


# ---------------------------------------------------------------- recipe

KEYFRAME_HEAD = "以参考图 1 的构图、机位、景别和光线为准出一张静帧"
KEYFRAME_PRODUCT = "，把画面里的产品换成参考图 2 的产品，保持参考图 2 产品的外形、颜色和 logo，不要变形"
PRODUCT_REFS_MAX = 3
PRODUCT_ROLES = ("整体外观", "质地/膏体", "背面或包装细节")


def product_ref_info(tpl: dict) -> tuple[int, list[str]]:
    """(how many product images the package expects, their roles). 0 when the product does not change."""
    if not tpl.get("needImage"):
        return 0, []
    info = tpl.get("productRefs") or (tpl.get("replica") or {}).get("productRefs") or {}
    count = max(1, min(PRODUCT_REFS_MAX, int(info.get("count") or 1)))
    roles = (list(info.get("roles") or []) + list(PRODUCT_ROLES))[:count]
    return count, roles


def keyframe_product_clause(refs: int, roles: list[str] | None = None) -> str:
    """The clause appended to every keyframe prompt, by how many product images the package expects."""
    refs = max(1, min(PRODUCT_REFS_MAX, int(refs or 1)))
    if refs == 1:
        return KEYFRAME_PRODUCT
    roles = (list(roles or PRODUCT_ROLES) + list(PRODUCT_ROLES))[:refs]
    numbers = f"参考图 2 至参考图 {refs + 1}" if refs > 2 else "参考图 2 和参考图 3"
    detail = "、".join(f"参考图 {j + 2} 是{roles[j]}" for j in range(1, refs))
    return (f"，把画面里的产品换成{numbers} 的产品（同一款产品：以参考图 2 为准，{detail}），"
            f"保持参考图 2 产品的外形、颜色和 logo，不要把几张图混成新的包装")
AUDIO_RULE = "声音只要人声口播和现场环境音，不要背景音乐。"


def video_prompt(video: str, line: str, voice: str) -> str:
    parts = [video]
    if line:
        parts.append(f"口播（{voice or '自然人声'}）：「{line}」")
    parts.append("产品保持外形和 logo，不要跳帧，画面不要字幕。" + AUDIO_RULE)
    return "\n".join(parts)


def build_recipe(recipe_id: str, name: str, video: str, meta: dict, units: list[dict], frames: list[str], seen: dict,
                 category: str = "", product_refs: int = 1, product_roles: list[str] | None = None) -> dict:
    need_image = bool(seen.get("needImage"))
    refs = max(1, min(PRODUCT_REFS_MAX, int(product_refs or 1))) if need_image else 0
    roles = (list(product_roles or []) + list(PRODUCT_ROLES))[:max(1, refs)]
    voice = seen.get("voice") or ""
    aspect = seen.get("aspectRatio") or meta.get("aspectRatio") or "9:16"
    if aspect not in ASPECTS:
        aspect = "9:16"
    texts = " ".join(f"{u['keyframe']} {u['video']}" for u in seen["units"])
    used = [slot for slot in seen.get("slots") or [] if f"【{slot['title']}】" in texts]
    slots = [{"key": f"s{k}", "title": slot["title"], **({"default": slot["default"]} if slot.get("default") else {})}
             for k, slot in enumerate(used, 1)]
    language = seen.get("language") or ""
    steps: list[dict] = []
    for unit, text, frame in zip(units, seen["units"], frames):
        k = unit["index"]
        line = stitch(text.get("line") or "", {}, slots)
        head = KEYFRAME_HEAD + (keyframe_product_clause(refs, roles) if need_image else "") + "。画面不要字幕。"
        steps.append({"id": f"u{k}-frame", "kind": "i2i", "title": f"{k}·{text['title']} 首帧", "imageFrom": "ref",
                      "refUrl": frame, "withProduct": need_image, "productRefs": refs, "productRoles": roles[:refs] if need_image else [],
                      "unit": k, "prompt": f"{head}\n{text['keyframe']}", "aspectRatio": aspect})
        steps.append({"id": f"u{k}-video", "kind": "i2v", "title": f"{k}·{text['title']} {unit['duration']:.1f}s",
                      "imageFrom": "prev", "unit": k, "duration": unit["generate"], "targetDuration": unit["duration"],
                      "prompt": video_prompt(text["video"], line, voice), "line": line, "aspectRatio": aspect})
    steps.append({"id": "assemble", "kind": "assemble", "title": "按原片时间线拼接", "audio": "generated", "prompt": ""})
    return {
        "id": recipe_id, "appId": recipe_id, "name": name,
        "desc": seen.get("desc") or f"按镜头复刻：{len(units)} 段，共 {float(meta.get('duration') or 0):.1f}s",
        "coverUrl": frames[0] if frames else "", "previewM3u8": video, "useCount": 0, "needImage": need_image,
        "slots": slots, "aspectRatio": aspect, "tags": ["自建", "按镜头复刻"], "origin": "draft", "mode": "replica",
        "category": category, "status": "ready", "createdAt": now(),
        "productRefs": {"count": refs, "roles": roles[:refs] if need_image else []},
        "replica": {
            "duration": round(float(meta.get("duration") or 0), 3), "width": meta.get("width"), "height": meta.get("height"),
            "fps": meta.get("fps"), "hasAudio": bool(meta.get("hasAudio")), "voice": voice, "language": language,
            "voiceId": "", "bgmUrl": "",
            "units": [{**unit, "refUrl": frame} for unit, frame in zip(units, frames)],
        },
        "steps": steps,
    }


def _slot_mark(titles: list[str]) -> str:
    if not titles:
        return ""
    return "【】" if len(titles) == 1 else "".join(f"【{t}】" for t in titles)


def shot_prompt(kind: str, note: str, slot_titles: list[str]) -> str:
    text = note.strip() or "补上这一镜在做什么"
    slot = _slot_mark(slot_titles)
    if kind == "t2i":
        return f"生成一张静帧。镜头：{text}。{f'可变内容：{slot}。' if slot else ''}画面不要字幕。".strip()
    if kind == "i2i":
        return f"保留图中产品的形状、外观和 logo，不要变形。这一镜：{text}。{f'可变要求：{slot}。' if slot else ''}".strip()
    return f"把上一张图做成视频。动作：{text}。产品保持外形和 logo，不要跳帧。{f'台词或画面文字：{slot}。' if slot else ''}".strip()


def build_whole_recipe(recipe_id: str, name: str, video: str, cover: str, seen: dict, category: str = "") -> dict:
    """draft.build_draft for the whole-video fallback: a chain of steps, the first image step uses the product."""
    slot_rows = [s for s in seen.get("slots") or [] if s.get("title")]
    titles = [s["title"] for s in slot_rows]
    need_image = bool(seen.get("needImage"))
    aspect = seen.get("aspectRatio") or "9:16"
    steps = []
    for index, shot in enumerate(seen["shots"]):
        kind = shot["kind"]
        step = {"id": f"s{index + 1}", "kind": kind, "title": shot.get("title") or f"第 {index + 1} 镜", "vendorHint": "",
                "prompt": shot.get("prompt") or shot_prompt(kind, shot.get("title") or "", titles), "aspectRatio": aspect}
        if kind != "t2i":
            if index == 0:
                if need_image:
                    step["imageFrom"] = "upload"
            else:
                step["imageFrom"] = "prev"
        if kind == "i2v" and shot.get("duration"):
            step["duration"] = shot["duration"]
        steps.append(step)
    slots = [{"key": f"s{i + 1}", "title": s["title"], **({"default": s["default"]} if s.get("default") else {})}
             for i, s in enumerate(slot_rows)]
    return {"id": recipe_id, "appId": recipe_id, "name": name,
            "desc": seen.get("desc") or "自建模板。参考片是上传的视频，提示词在流程图里。",
            "coverUrl": cover, "previewM3u8": video, "useCount": 0, "needImage": need_image, "slots": slots,
            "aspectRatio": aspect, "tags": ["自建"], "origin": "draft", "category": category, "status": "ready",
            "createdAt": now(), "steps": steps}


# ---------------------------------------------------------------- slots

PLACEHOLDER = re.compile(r"【([^】]*)】")


def _slot_value(slot: dict, values: dict) -> str:
    typed = str(values.get(slot.get("key", ""), "") or "").strip()
    return typed or str(slot.get("default") or "").strip()


def stitch(template: str, values: dict | None, slots: list[dict] | None) -> str:
    values = values or {}
    slots = slots or []
    filled = [_slot_value(slot, values) for slot in slots]
    unnamed = 0

    def lookup(name: str):
        for slot in slots:
            if slot.get("key") == name:
                return _slot_value(slot, values)
        for slot in slots:
            if slot.get("title") == name:
                return _slot_value(slot, values)
        return None

    def replace(match: re.Match) -> str:
        nonlocal unnamed
        name = (match.group(1) or "").strip()
        if name:
            hit = lookup(name)
            return match.group(0) if hit is None else hit
        if not filled:
            return ""
        if len(filled) == 1:
            return filled[0]
        value = filled[min(unnamed, len(filled) - 1)]
        unnamed += 1
        return value

    return PLACEHOLDER.sub(replace, template or "")


def slot_values(recipe: dict, raw: dict) -> dict:
    """Accept values keyed by slot key (s1) or by title (食材)."""
    out = {}
    for slot in recipe.get("slots") or []:
        for name in (slot.get("key"), slot.get("title")):
            if name in raw and str(raw[name]).strip():
                out[slot["key"]] = str(raw[name]).strip()
    return out


# ---------------------------------------------------------------- lines

REWRITE_SYSTEM = "你是短视频口播编辑。只按要求替换内容，不改风格、不加内容。只输出一个 JSON 对象。"


def rewrite_request(lines: list[str], changes: list[dict], language: str) -> str:
    name = LANGUAGE_NAMES.get(language, "台词原来的语言")
    payload = {"changes": [{"槽位": c["title"], "原值": c["old"], "新值": c["new"]} for c in changes], "lines": lines}
    return (
        f"角色：{REWRITE_SYSTEM}\n\n"
        f"下面是一条视频每段的口播台词（{name}），以及用户换掉的内容。\n"
        "把台词里提到原值的地方换成新值；新值不是这门语言时翻译成这门语言再用。"
        "原值在台词里分散在几段说（每段说其中几项）时，新值也按段分开说，不要每段都把新值整串念一遍。"
        "原值里新值没有对应的项（比如原来 4 种食材、现在只有 3 种）要从台词里删掉，不能留下任何原值项。"
        "删掉后句子不完整时只做最小修补（比如去掉多余的连接词），不要补出画面里没有的新动作，也不要重复别的段已经说过的话。"
        "没提到原值的台词一个字都不要改；每段长度和原来差不多；空台词保持为空。\n"
        '输出：{"lines":["第1段","第2段",...],"translated":{"槽位":"新值翻译成台词语言后的写法"}}，lines 数量和输入一致。\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )


def leftover_items(lines: list[str], changes: list[dict]) -> list[str]:
    left = []
    for change in changes:
        new = str(change.get("new") or "").lower()
        for item in re.split(ITEM_SPLIT, str(change.get("old") or "")):
            item = item.strip().lower()
            if len(item) < 3 or item in new:
                continue
            pattern = rf"(?<![a-z]){re.escape(item)}(?![a-z])" if item.isascii() else re.escape(item)
            if any(re.search(pattern, line.lower()) for line in lines) and item not in left:
                left.append(item)
    return left


# ---------------------------------------------------------------- template


class TemplateError(ValueError):
    pass


CATEGORY_MAX = 12


def clean_category(value) -> str:
    return " ".join(str(value or "").split())[:CATEGORY_MAX]


def _replace_all(steps: list[dict], old: str, new: str) -> None:
    for step in steps:
        if old in (step.get("prompt") or ""):
            step["prompt"] = step["prompt"].replace(old, new)


def build_template(recipe: dict, job: dict | None, name: str, desc: str = "", category=None,
                   slot_choices: list[dict] | None = None, keep_units: list[int] | None = None) -> dict:
    if recipe.get("origin") not in {"draft", "template"} or recipe.get("status") in {"analyzing", "failed"}:
        raise TemplateError("只有拆好的配方能存为模板")
    name = (name or "").strip()
    if not name:
        raise TemplateError("模板要有名字")
    tpl = copy.deepcopy(recipe)
    steps = tpl.get("steps") or []
    values = (job or {}).get("slots") or {}
    choices = {item.get("key"): item for item in slot_choices or [] if isinstance(item, dict)}
    slots = []
    for slot in tpl.get("slots") or []:
        choice = choices.get(slot.get("key"), {"keep": True})
        value = _slot_value(slot, values)
        title = slot.get("title") or ""
        if not choice.get("keep", True):
            _replace_all(steps, f"【{title}】", value)
            _replace_all(steps, f"【{slot.get('key')}】", value)
            continue
        new_title = str(choice.get("title") or title).strip()[:24] or title
        if new_title != title:
            _replace_all(steps, f"【{title}】", f"【{new_title}】")
        kept = {"key": slot["key"], "title": new_title}
        if value:
            kept["default"] = value
        slots.append(kept)
    tpl["slots"] = slots
    if tpl.get("mode") == "replica" and keep_units is not None:
        keep = {int(k) for k in keep_units}
        units = [u for u in (tpl.get("replica") or {}).get("units") or [] if int(u["index"]) in keep]
        if not units:
            raise TemplateError("至少保留一段")
        steps = [s for s in steps if s.get("unit") is None or int(s["unit"]) in keep]
        tpl["replica"] = {**tpl["replica"], "units": units, "duration": round(sum(float(u["duration"]) for u in units), 3)}
    tpl["steps"] = steps
    if job:
        first = next((s.get("outputUrl") for s in job.get("steps") or [] if s.get("kind") in {"i2i", "t2i"} and s.get("outputUrl")), "")
        tpl["coverUrl"] = first or tpl.get("coverUrl") or ""
        tpl["sampleUrl"] = job.get("resultUrl") or ""
        tpl["sourceJobId"] = job.get("id")
    tpl.update({
        "id": f"tpl-{nid()}", "name": name[:40], "desc": (desc or "").strip()[:160] or tpl.get("desc") or "",
        "origin": "template", "status": "ready", "useCount": 0,
        "category": clean_category(recipe.get("category") if category is None else category),
        "tags": list(dict.fromkeys(["模板", *(["按镜头复刻"] if tpl.get("mode") == "replica" else [])])),
        "sourceRecipeId": recipe.get("id"), "createdAt": now(),
    })
    tpl["appId"] = tpl["id"]
    tpl.pop("source", None)
    tpl.pop("error", None)
    return tpl
