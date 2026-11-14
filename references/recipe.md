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
{ schema: "video-replica-handoff", version: 2, instructions: "AGENT.md", diagram: "flow.html",
  inputs: { productImage: "required"|"none",
            products: [{key: "p1", label: "卸妆膏", where: "画面右侧", image: "<productImage>"},
                       {key: "p2", label: "化妆棉", where: "右下角", image: "<productImage:2>"}],   // needImage 时才有
            slots: [{key, title, default}] },
  output: { width, height, aspectRatio, duration, fpsMax: 30 },
  units: [{ index, sourceStart, sourceEnd, targetDuration,
            frame: { step, kind: "image-to-image",
                     references: ["frames/u1-first.jpg", "<productImage>", "<productImage:2>"],  // 首帧 + 本镜出现的产品，顺序即参考图 2、3
                     prompt, aspectRatio },
            video: { step, kind: "image-to-video", firstFrame: "<output of u1-frame>", duration, prompt, line, aspectRatio } }],
  assembly: { order: "by unit index", speedRange: [0.8, 3], fit: "scale-crop-fill", useReferenceAudio: false, bgmVolume: 0.35 },
  rules: [...] }
```

## 口播输入

`transcript_json=[{"start": 0.2, "end": 3.0, "text": "…"}]`（秒），或 `.srt` / `.vtt` / `.json` / `.tsv`（毫秒）。
一句跨两段时按重叠时长把词切开；沾边不到 25% 的段不分词。语言自动识别为 zh / en / es。

## 同一镜头里多件产品

一段画面里可能同时出现两件产品（卸妆膏和化妆棉同框、双支套装并排、主品和工具一起入镜）。这种镜头只给一张产品图是不行的：模型会把第二件产品留成原片里的样子。做法是**每件产品一张参考图**。

模型回答里两个字段描述产品：

```jsonc
"products": [{"name": "卸妆膏", "where": "画面右侧"}, {"name": "化妆棉", "where": "右下角"}],
"units": [{"title": "…", "products": ["卸妆膏", "化妆棉"], "keyframe": "…", "video": "…"}]
```

- `products` 是整个片子出现的不同产品，顺序只决定 handle：`p1` → `<productImage>`，`p2` → `<productImage:2>`，最多 3 件。
  「参考图 N」只按**每一镜内部**的顺序写（见下），所以 `AGENT.md` 的输入清单用 handle 列产品，不用「参考图 N」。
- 每段的 `products` 只列这一段画面里真有的产品；没写时按第一件处理（老回答照样能跑）。
- 提示词按这一段的产品数量改写：一件时照旧「把画面里的产品换成参考图 2 的产品」；两件时「画面里有 2 件产品：画面右侧的卸妆膏换成参考图 2 的卸妆膏，画面右下角的化妆棉换成参考图 3 的化妆棉；每件各自保持外形、颜色和 logo，不要混成一件，也不要多出别的产品」。视频提示词同步改成「2 件产品各自保持外形和 logo，位置和数量都不要变」。
- **参考图编号是每一镜内部的顺序**：某一段只出现化妆棉时，它的 `references` 是 `[首帧, "<productImage:2>"]`，提示词里的「参考图 2」就指这一张——`AGENT.md` 逐段写清楚了哪张图是哪件产品。
- CLI 可以覆盖模型给的张数和名字：`--products 2 --product-labels "卸妆膏,化妆棉"`（`--product-images` / `--product-roles` 是旧别名）。

## 产品仍然不做槽位

产品外形只来自上传的产品图。`product / 商品 / 产品名 / 主产品` 以及槽值里被判定为产品原型的条目会被丢掉，`recipe` 的 `warnings` 会说明原因和替代做法（把包装细节拍进产品图，不要用文字描述）。
