# 文件和字段

## 工作目录

```
replica-work/
  source/<参考视频>
  analysis.json        ingest 结果：meta、shots、units、frames、speech
  frames/u<k>-first.jpg  每段首帧（i2i 的参考图 1）
  request.md           给你看的拆片请求（看片写配方用）
  vlm.json             你的回答（recipe 读它）
  recipe.json          配方 = 模板（origin=draft）
  slotify_request.md   槽位原值没替换干净时才有
  export/<tpl-id>/     交付包：template.json、AGENT.md、flow.html、frames/（不含参考片）
  export/<tpl-id>.zip  同一个交付包的 zip
```

## recipe.json（与内容复刻台配方字段一致）

```
{ id: "draft-…", name, desc, coverUrl, previewM3u8: "source/…mp4", needImage, aspectRatio,
  slots: [{key: "s1", title: "食材", default: "banana、spinach"}],
  mode: "replica", origin: "draft"|"template", category, tags,
  replica: { duration, width, height, fps, hasAudio, voice, language, voiceId: "", bgmUrl: "",
             units: [{index, start, end, duration, generate, transitionIn, shots: [{start,end,transition}], speech?, refUrl}] },
  steps: [
    {id: "u1-frame", kind: "i2i", imageFrom: "ref", refUrl: "frames/u1-first.jpg", withProduct, unit: 1, prompt, aspectRatio},
    {id: "u1-video", kind: "i2v", imageFrom: "prev", unit: 1, duration: 生成秒数, targetDuration: 原片秒数, prompt, line, aspectRatio},
    …,
    {id: "assemble", kind: "assemble", audio: "generated"}
  ] }
```

- 首帧提示词固定开头：「以参考图 1 的构图、机位、景别和光线为准出一张静帧，把画面里的产品换成参考图 2 的产品，保持参考图 2 产品的外形、颜色和 logo，不要变形。画面不要字幕。」
- 视频提示词 = 你写的 video + 「口播（音色）：「台词」」+「产品保持外形和 logo，不要跳帧，画面不要字幕。声音只要人声口播和现场环境音，不要背景音乐。」
- 整片模式（`mode` 不是 replica）：steps 是最多 6 步的链（t2i/i2i/i2v），第一张图用产品图，后面每步用上一步结果。

## 交付包 template.json

和 recipe.json 同样的字段，另外：

- `origin: "template"`，`id: "tpl-…"`；`refUrl` / `coverUrl` 指向包内 `frames/…`，`previewM3u8` 为空（参考片不随包附带）。
- `handoff`（机器可读的执行摘要）：

```
{ schema: "video-replica-handoff", version: 1, instructions: "AGENT.md", diagram: "flow.html",
  inputs: { productImage: "required"|"none",
            productImages: { count: 1|2|3, min: 1, max: 3, roles: ["整体外观", …] },   // needImage 时才有
            slots: [{key, title, default}] },
  output: { width, height, aspectRatio, duration, fpsMax: 30 },
  units: [{ index, sourceStart, sourceEnd, targetDuration,
            frame: { step, kind: "image-to-image",
                     references: ["frames/u1-first.jpg", "<productImage>", "<productImage:2>", "<productImage:3>"], prompt, aspectRatio },
            video: { step, kind: "image-to-video", firstFrame: "<output of u1-frame>", duration, prompt, line, aspectRatio } }],
  assembly: { order: "by unit index", speedRange: [0.8, 3], fit: "scale-crop-fill", useReferenceAudio: false, bgmVolume: 0.35 },
  rules: [...] }
```

## 口播输入

`transcript_json=[{"start": 0.2, "end": 3.0, "text": "…"}]`（秒），或 `.srt` / `.vtt` / `.json` / `.tsv`（毫秒）。
一句跨两段时按重叠时长把词切开；沾边不到 25% 的段不分词。语言自动识别为 zh / en / es。

## 产品图 1–3 张

产品外形只来自上传的产品图，所以图越多，模型猜得越少：正面决定外形、颜色、logo，第 2 张补质地，第 3 张补背面或成分表。

```bash
python3 scripts/desk.py recipe WD --vlm vlm.json --product-images 3 --product-roles "整体外观,膏体质地,背面成分表"
```

- 张数写进 `recipe.productRefs = {count, roles}`，随模板进 `template.json`，`handoff.inputs.productImages` 同源。
- 首帧参考图变成 `[frames/u1-first.jpg, "<productImage>", "<productImage:2>", "<productImage:3>"]`；只给 1 张时仍是 `[首帧, "<productImage>"]`，与旧包兼容。
- 首帧提示词按张数改写：「参考图 2 至参考图 4 的产品（同一款产品：以参考图 2 为准，参考图 3 是膏体质地、参考图 4 是背面成分表）…不要把几张图混成新的包装」。
- `package` 读配方里的张数，不用再传参数；改张数就重跑 `recipe`。
- **产品仍然不做槽位**：`product / 商品 / 产品名 / 主产品` 以及槽值里被判定为产品原型的条目会被丢掉，`recipe` 的 `warnings` 会说明原因。外形细节请拍进产品图，不要用文字描述。
