---
name: video-replica-template
description: >-
  Turn a reference short video into a shot-by-shot replica template package: shot detection,
  per-unit keyframe + video prompts, slots, handoff zip — for another agent to execute with
  its own image / video model. Any Chinese-trigger phrases below also apply.
  复刻模板：把一条参考短视频（TikTok / Reels / 抖音带货片）拆成按镜头复刻的模板——按原片节奏切成 4–15 秒、
  最多 6 个生成段，每段抽首帧、写「首帧图生图」和「图生视频」提示词、配台词，把换产品时要跟着换的内容做成槽位，
  最后导出交付包（template.json + AGENT.md + flow.html + 首帧 + zip）交给有生图/生视频能力的执行 agent。
  用户给一条视频说「做成复刻模板」「新增模板」「拆成模板交给生成 agent」「按这条片复刻」时使用。
  本 skill 只做拆片、写配方、打包，不生成图片或视频。
---

# 复刻模板：参考视频 → 交付包

目标：把参考片变成一份**执行 agent 照着就能逐段生成**的模板。不要把整条视频交给视频模型说「换产品」——那样产品、构图、节奏都会飘。

工具：`scripts/desk.py`，每个子命令输出**一个 JSON**；`ok=false` 时读 `error` 修了再调。

## 前置

本 skill 不依赖运行时，只要有 `python3` + `ffmpeg`；任何能跑 shell、能读写文件的 agent 都能用（Claude Code、
DSH、Codex CLI、Cursor…）。下文用 `$SKILL` 表示**本 SKILL.md 所在目录**，按你的安装位置替换：

```bash
SKILL=~/.claude/skills/video-replica-template     # Claude Code
SKILL=~/.dsh/skills/video-replica-template        # DSH
SKILL=/path/to/video-replica-template             # 手动克隆
```

```bash
python3 "$SKILL/scripts/desk.py" doctor
```

- 需要 `python3`（3.9+，只用标准库）和 `ffmpeg` / `ffprobe`。缺 ffmpeg 就没法切镜、抽首帧，直接告诉用户装。
- 切镜默认纯 Python（`lite`，零依赖）：认**硬切、跳切、闪切、淡入淡出**；**叠化、划像切不开**，这类片子相邻镜头
  会被并成一段。想让叠化/划像也切开，装上 numpy + OpenCV，检测器自动换成 `cv2`：

  ```bash
  pip install numpy opencv-python-headless
  ```

  `cv2` 检测器多做的事：叠化、划像、MG 卡片换页（文字卡/版式卡的停顿切换）、光流校验（手摇、横移、
  视差不再误判成切点）、闪光转场单独标 `flash`。

- `doctor` 的 `detector` 字段显示当前用哪个（`cv2` / `lite`），换 Python 环境要重跑 `doctor` 确认。

## 路径约定

- **SKILL**：本 SKILL.md 所在目录，命令里一律用绝对路径展开。
- **WD**：工作目录，建议 `<当前项目>/replica-work`（绝对路径）。
- `--vlm`、`--transcript` 是**相对 WD** 的路径：传 `vlm.json`，不要传 `replica-work/vlm.json`。
- 目录结构：`source/`（参考片）、`analysis.json`、`frames/u<k>-first.jpg`、`request.md`、`vlm.json`、`recipe.json`、`export/<模板id>/`。

## 步骤

### 1. 拆片

```bash
python3 "$SKILL/scripts/desk.py" ingest "<参考视频>" "<WD>" --name "<模板名>" --category "<品类>"
```

切镜 → 同场景连续硬切并段 → 分成 4–15 秒、最多 6 段的生成段 → 每段抽首帧 → 写 `request.md`。

返回里的关键字段：`mode`、`units[]`（`start/end/duration/generate/shots/frameTime/look`）、`warnings`。

`look` 是逐帧量出来的画面特征：`brightness`（0–100）、`contrast`、`saturation`、`warmth`（正数偏暖、负数偏冷）、
`motion`（`static` 固定 / `pan` 横移 / `zoom` 推拉 / `handheld` 手持晃动）、`motionStrength`。
首帧取在开场转场结束之后（`frameTime`），不会是叠化的混合帧。相邻段色温或亮度跳变很大时 `warnings` 会提示。
`mode=whole` 表示片子超过 6×15 秒或切镜不可用（整片模式导出不了交付包）——把原因告诉用户，问要不要截取一段或调 `--max-units`。

### 2. 看片（必须真看，不能猜）

- 打开参考片整体看一遍，注意节奏、动作、运镜、贴纸出现的时机。
- **逐张**打开 `frames/u1-first.jpg … uN-first.jpg`（用你的看图工具：读图、附件、或直接打开文件）。
- 需要中间帧时自己抽，别凭想象：

```bash
ffmpeg -v error -y -ss <秒> -i "<参考视频>" -frames:v 1 -vf scale=720:-1 "<WD>/frames/probe.jpg"
```

- `units[]` 的 `look` 已经给出运镜和明暗，写提示词时照它写，不要凭首帧猜（静帧看不出推拉）。
- 想看清某段内部怎么切，按 2fps 铺一张图最快：

```bash
ffmpeg -v error -y -ss <段起点> -t <段时长> -i "<参考视频>" \
  -vf "fps=2,scale=200:-1,tile=6x3:padding=3:color=white" -frames:v 1 "<WD>/frames/sheet.jpg"
```

### 3. 口播（有人声才做）

把原片口播转写成带秒数的句子，写成 JSON 再挂上去：

```json
[{"start": 0.02, "end": 4.02, "text": "卸妆卸不对，真的会越卸越丑"}]
```

```bash
python3 "$SKILL/scripts/desk.py" speech "<WD>" --transcript transcript.json
```

- 只有**听到的声音**才算口播；画面上的字幕、花字、贴纸不是口播。
- 模型听不了音频时：要么跳过这一步（`line` 全部留空字符串），要么用本地 ASR 转（可选，重）：

```bash
pip install faster-whisper   # 首次会下模型；转写质量一般，转完要人眼校错字
ffmpeg -v error -y -i "<参考视频>" -ac 1 -ar 16000 audio.wav
```

- 语言自动识别为中文 / 日语 / 英语 / 西语（zh / ja / en / es）；一句跨两段时工具按重叠时长切词。

### 4. 写配方（vlm.json）

读 `<WD>/request.md`——里面有序号、每段时长和镜头表，以及**8 条规则**。按它要求的结构写 `vlm.json`：

```json
{"desc": "一句话", "needImage": true, "aspectRatio": "9:16",
 "product": {"name": "品牌+品类", "aliases": ["画面里的其他叫法"]},
 "products": [{"name": "卸妆膏", "where": "画面右侧"}, {"name": "化妆棉", "where": "右下角"}],
 "voice": "年轻女声，语速偏快、亲切",
 "slots": [{"title": "人物", "default": "参考片里的原值"}],
 "units": [{"title": "≤8 字", "keyframe": "首帧静帧提示词", "video": "视频提示词", "line": "这段台词",
            "products": ["这一段画面里出现的产品"]}]}
```

写作要点（其余见 `references/recipe.md`）：

- `units` 数量必须和段表一致，顺序一致，不合并不拆分。
- `keyframe`：景别、机位角度、构图、场景、光线、人物和产品的位置。生成时会把你写的和原片首帧一起给模型，所以**照首帧写**，别写别的时刻。
- `video`：先景别和运镜（推/拉/摇/移/跟/固定/手持），再写动作过程；段内多镜头按 `镜头1（0.0–4.1s）：…；镜头2（4.1–5.3s）：…` 逐个写，最后一句写结束状态。
- 产品一律写「**上传的产品**」，不写参考片的品牌、颜色、包装；场景里的颜色照写。
- 换产品时要跟着换的内容做成 `slots`（最多 5 个），提示词里写 `【槽名】`；**产品本身不做槽位**（做成了也会被丢掉并给 warning）。槽名 2–4 字。
- 产品外形只来自上传的产品图，**每件产品一张**：一段画面里同框两件产品（卸妆膏 + 化妆棉、双支套装）时，每件都要一张图，提示词里按「参考图 2 / 参考图 3」分别对应，不要让模型拿一件顶两件。`vlm.json` 的 `products` 列出所有产品，每段 `products` 只列这一段真有的。
- `line`：把这段口播改写成台词——同语言、说得完、提到产品用品类说法（如「这罐卸妆膏」），不出现原品牌。没有口播给空字符串。
- 提示词里不写字幕、花字、贴纸，不写分析过程。

### 5. 组装配方

```bash
python3 "$SKILL/scripts/desk.py" recipe "<WD>" --vlm vlm.json
```

返回 `ok=false` 通常是 `vlm.json` 结构或 `units` 数量不对；`warnings` 里提到「还写着槽位原值」时，说明提示词里有漏掉的原值——按 `<WD>/slotify_request.md` 改成 `【槽名】` 再调一次。

### 6. 给用户过目

列出来：每段（时间、标题、首帧/视频提示词要点、台词）+ 槽位表，问要不要改槽名、去掉哪些段、把哪些槽位写死。用户确认后再导出。

### 7. 导出交付包

```bash
python3 "$SKILL/scripts/desk.py" package "<WD>" \
  --name "模板名" --category "品类" --desc "一句话描述" \
  [--units 1,2,4] [--slots '[{"key":"s1","title":"出镜人"},{"key":"s2","keep":false}]']
```

产出 `export/<模板id>/`：`template.json`（模板本体 + `handoff` 机器可读摘要）、`AGENT.md`（逐段执行说明）、
`flow.html`（单文件流程图，可拖动、点节点看完整提示词）、`frames/`，以及同名 zip。参考片不随包附带。

### 8. 交付

告诉用户：zip 路径、执行 agent 需要的输入（产品图是否必填、有哪些槽位和默认值）、以及每段要生成几秒。

## 规则

- 时间、首帧、口播都来自工具和视频本身；看不清就少写，**不要编**。
- 不要手改 `recipe.json` 里 `replica.units` 的时间——要改分段就重新 ingest（`--max-units` 可调）。
- `mode=whole` 不能导出交付包，别硬凑。
- 只处理用户提供或有权使用的视频；交付包里不放参考片本身。
- 交付包路径全是相对路径、文件夹名只用 ASCII，解压到哪都能用。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 找不到 `vlm.json` | `--vlm` 是相对 WD 的路径，写 `vlm.json` |
| `units 数量不一致` | `vlm.json` 的 `units` 必须正好等于段表段数 |
| `mode=whole` | 片子太长或切镜不可用；截短素材或调 `--max-units` |
| 叠化/划像处没切开 | 当前是 `lite`（没装 cv2）；`pip install numpy opencv-python-headless` 后重跑 `ingest`，或截出该段单独跑 |
| 交付包里出现原品牌 | 提示词里还有原值；`recipe` 的 `warnings` 会提示，按 `slotify_request.md` 改 |

## 目录

```
SKILL.md                  本文件
references/recipe.md      文件与字段清单（recipe.json / template.json / handoff）
references/workflow.md    流程图和每一步为什么这么做
scripts/desk.py           命令行入口：doctor / ingest / speech / recipe / package / status
scripts/desklib/          media、plan（分段/口播）、profile（画面统计）、recipe（配方/槽位）、
                          handoff（交付包）、flowhtml、shots_lite（纯标准库切镜）、shots_cv（叠加层）
scripts/selftest.py       自检：切镜、分段、口播分配、画面统计、配方、交付包、ffmpeg 端到端
```

## 示例（一条 43 秒带货片走完全流程）

```bash
SKILL=/path/to/video-replica-template      # 见「前置」
WD=~/replica-work
python3 "$SKILL/scripts/desk.py" doctor
python3 "$SKILL/scripts/desk.py" ingest ~/Downloads/ref.mp4 "$WD" --name "国货卸妆膏-痛点种草" --category "美妆"
# 看 frames/u*-first.jpg → 写 "$WD/vlm.json" → 可选 speech
python3 "$SKILL/scripts/desk.py" recipe "$WD" --vlm vlm.json
python3 "$SKILL/scripts/desk.py" package "$WD" --name "国货卸妆膏-痛点种草" --category "美妆个护"
```
