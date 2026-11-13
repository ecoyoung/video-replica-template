#!/usr/bin/env python3
"""Shot-based video replica, standalone (content-desk workflow).

  desk.py doctor
  desk.py ingest   VIDEO WD [--name N] [--category C] [--transcript FILE] [--max-units 6] [--mode auto|units|whole]
  desk.py speech   WD --transcript FILE                     # attach the reference voice-over (srt/vtt/json/tsv)
  desk.py recipe   WD --vlm vlm.json [--name N]             # agent's answer to request.md -> recipe.json (the template)
  desk.py package  WD [--name N] [--desc D] [--category C] [--units 1,2] [--slots JSON] [--out DIR]
                                                           # handoff package: template.json + AGENT.md + flow.html + frames/ (+ zip)
  desk.py status   WD

Every command prints one JSON object on stdout ({"ok": true, ...} or {"ok": false, "error": ...}).
This tool only builds the template; image / video generation is done by whoever receives the package.
Standard library only; ffmpeg/ffprobe for ingest; numpy + opencv-python optional (better shot detection).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desklib import handoff, media, plan  # noqa: E402
from desklib import profile as look  # noqa: E402
from desklib import recipe as rcp  # noqa: E402

FRAME_WIDTH = 720


class Fail(Exception):
    pass


def out(payload: dict) -> None:
    sys.stdout.write(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Fail(f"没有 {what}：{path}") from None
    except ValueError as exc:
        raise Fail(f"{what} 不是合法 JSON：{exc}") from None


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def json_arg(text: str | None, what: str, default):
    if text is None or text == "":
        return default
    candidate = Path(text)
    if not text.lstrip().startswith(("{", "[")) and candidate.is_file():
        text = candidate.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise Fail(f"{what} 不是合法 JSON：{exc}") from None


def analysis_path(wd: Path) -> Path:
    return wd / "analysis.json"


def load_analysis(wd: Path) -> dict:
    return read_json(analysis_path(wd), "analysis.json（先 ingest）")


def rel(wd: Path, path: Path) -> str:
    return str(path.resolve().relative_to(wd.resolve()))


# ---------------------------------------------------------------- doctor


def cmd_doctor(_args) -> None:
    import platform

    out({
        "python": platform.python_version(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "detector": plan.detector_name(),
        "notes": [
            "detector=cv2：硬切/跳切/闪切/淡入淡出 + 叠化/划像" if plan.detector_name() == "cv2"
            else "detector=lite：只认硬切/跳切/闪切/淡入淡出；pip install numpy opencv-python-headless 后可换 cv2 检测器（多认叠化/划像）",
            "没有 ffmpeg 时无法切镜和抽首帧，交付包需要按镜头的模板，请先装 ffmpeg",
        ],
    })


# ---------------------------------------------------------------- ingest / speech


def _write_request(wd: Path, analysis: dict) -> Path:
    path = wd / "request.md"
    video = str((wd / analysis["video"]).resolve())
    if analysis["mode"] == "units":
        frames = [str((wd / f).resolve()) for f in analysis["frames"]]
        text = rcp.request_markdown(analysis["name"], analysis["units"], analysis["meta"], frames, video)
    else:
        text = rcp.whole_request_markdown(analysis["name"], video)
    path.write_text(text, encoding="utf-8")
    return path


def _apply_speech(analysis: dict, segments: list[dict]) -> None:
    analysis["speech"] = {"segments": segments, "language": plan.speech_language(segments)}
    if analysis["mode"] != "units":
        return
    for unit in analysis["units"]:
        unit.pop("speech", None)
    for unit, text in zip(analysis["units"], plan.assign_speech(segments, analysis["units"])):
        if text:
            unit["speech"] = text


def cmd_ingest(args) -> None:
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise Fail(f"找不到视频：{video}")
    wd = Path(args.workdir).expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    inside = wd in video.parents
    if not inside:
        safe = re.sub(r"[^\w.\-]+", "_", video.name) or "reference.mp4"
        dest = wd / "source" / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.stat().st_size != video.stat().st_size:
            shutil.copyfile(video, dest)
        video = dest
    name = (args.name or video.stem).strip()[:60] or "复刻模板"
    frames_dir = wd / "frames"
    if frames_dir.exists():
        for old in frames_dir.glob("u*-first.jpg"):
            old.unlink()
    warnings: list[str] = []
    analysis = {"schema": "desk.analysis", "version": 1, "name": name, "category": rcp.clean_category(args.category),
                "video": rel(wd, video), "createdAt": rcp.now(), "warnings": warnings}
    mode = args.mode
    if mode in ("auto", "units"):
        try:
            meta, rows, detector = plan.detect_shots(video)
            units = plan.plan_units(rows, limit=args.max_units)
            profiles = look.shot_profiles(video, meta, rows)
            for row, prof in zip(rows, profiles):
                if prof:
                    row["look"] = prof
            for unit in units:
                unit["look"] = look.unit_look(profiles, rows, unit)
            warnings.extend(look.warnings(units))
            frames = []
            for unit in units:
                t = plan.first_frame_time(unit, rows)  # after the transition, never a blend frame
                unit["frameTime"] = t
                frame = media.extract_frame(video, t, frames_dir / f"u{unit['index']}-first.jpg", width=FRAME_WIDTH)
                frames.append(rel(wd, frame))
            analysis.update({"mode": "units", "meta": meta, "detector": detector, "shots": plan.public_shots(rows),
                             "units": units, "frames": frames})
            if detector == "lite":
                warnings.append("切镜用的是标准库检测（硬切/跳切/闪切/淡入淡出），叠化和划像不会切开；装 numpy+opencv 后重新 ingest 可完整检测")
        except plan.ShotAnalysisError as exc:
            if mode == "units":
                raise Fail(str(exc)) from None
            warnings.append(f"按镜头拆不了，退回整片模式：{exc}")
            mode = "whole"
    if mode == "whole":
        meta = {}
        try:
            meta = media.probe(video)
        except Exception as exc:  # noqa: BLE001 - no ffprobe / unreadable: the agent still watches the video
            warnings.append(f"读不了视频信息：{exc}")
        cover = ""
        if media.have_tools():
            try:
                cover = rel(wd, media.extract_frame(video, 0.5, frames_dir / "cover.jpg", width=FRAME_WIDTH))
            except Exception:  # noqa: BLE001
                cover = ""
        analysis.update({"mode": "whole", "meta": meta, "detector": "", "shots": [], "units": [], "frames": [], "cover": cover})
    if args.transcript:
        _apply_speech(analysis, plan.parse_transcript(Path(args.transcript).expanduser()))
    write_json(analysis_path(wd), analysis)
    request = _write_request(wd, analysis)
    out({
        "workdir": str(wd), "mode": analysis["mode"], "detector": analysis.get("detector"), "meta": analysis["meta"],
        "shots": len(analysis["shots"]),
        "units": [{"index": u["index"], "start": u["start"], "end": u["end"], "duration": u["duration"], "generate": u["generate"],
                   "shots": len(u["shots"]), "frame": str((wd / f).resolve()), "speech": u.get("speech", "")}
                  for u, f in zip(analysis["units"], analysis["frames"])],
        "request": str(request), "warnings": warnings,
        "next": ("先给原片口播：有语音就转写成带时间的句子再跑 speech（没有口播跳过）；然后读 request.md，看视频和每段首帧，"
                 "把回答的 JSON 写进 vlm.json，再跑 recipe。"),
    })


def cmd_speech(args) -> None:
    wd = Path(args.workdir).expanduser().resolve()
    analysis = load_analysis(wd)
    segments = plan.parse_transcript(Path(args.transcript).expanduser())
    _apply_speech(analysis, segments)
    write_json(analysis_path(wd), analysis)
    request = _write_request(wd, analysis)
    out({"segments": len(segments), "language": analysis["speech"]["language"],
         "units": [{"index": u["index"], "speech": u.get("speech", "")} for u in analysis["units"]], "request": str(request)})


# ---------------------------------------------------------------- recipe


def cmd_recipe(args) -> None:
    wd = Path(args.workdir).expanduser().resolve()
    analysis = load_analysis(wd)
    raw = args.vlm_json
    if raw is None:
        path = Path(args.vlm).expanduser() if args.vlm else wd / "vlm.json"
        if not path.is_absolute():
            path = wd / path
        if not path.is_file():
            raise Fail(f"没有模型回答：{path}（读 request.md，把回答的 JSON 写进 vlm.json）")
        raw = path.read_text(encoding="utf-8")
    try:
        answer = rcp.parse_object(raw)
    except ValueError as exc:
        raise Fail(str(exc)) from None
    existing = wd / "recipe.json"
    old = read_json(existing, "recipe.json") if existing.is_file() else {}
    recipe_id = old.get("id") or f"draft-{rcp.nid()}"
    name = (args.name or analysis["name"]).strip()[:60]
    warnings = []
    try:
        if analysis["mode"] == "units":
            seen = rcp.normalize_units_answer(answer, analysis["units"])
            language = (analysis.get("speech") or {}).get("language") or ""
            seen["language"] = language
            if language and any(u.get("line") for u in seen["units"]):
                seen["voice"] = "，".join(x for x in (seen.get("voice"), plan.LANGUAGE_NAMES[language]) if x)
            seen = rcp.slotify_fallback(rcp.drop_product_slots(seen))
            dropped = seen.get("droppedProductSlots") or {}
            for title in dropped.get("slots") or []:
                warnings.append(f"槽位「{title}」被丢掉：产品不能做槽位，外形只能靠上传的产品图；"
                                "包装背面、质地这类信息请拍成第 2、3 张产品图（recipe --product-images 2/3），不要用文字描述")
            for title in dropped.get("items") or []:
                warnings.append(f"槽位「{title}」里的产品项被移除了：产品不能做槽位，剩下的内容已保留")
            left = rcp.unslotted(seen)
            if left:
                (wd / "slotify_request.md").write_text(rcp.slotify_request(seen), encoding="utf-8")
                warnings.append(f"提示词里还写着槽位原值 {left}：按 slotify_request.md 改写 vlm.json 里的 keyframe/video 后重跑 recipe")
            labels = [x.strip() for x in str(args.product_labels or "").split(",") if x.strip()]
            recipe = rcp.build_recipe(recipe_id, name, analysis["video"], analysis["meta"], analysis["units"], analysis["frames"],
                                      seen, analysis.get("category") or "",
                                      product_count=args.products, product_labels=labels)
        else:
            seen = rcp.normalize_recipe(answer)
            recipe = rcp.build_whole_recipe(recipe_id, name, analysis["video"], analysis.get("cover") or "", seen,
                                            analysis.get("category") or "")
    except ValueError as exc:
        raise Fail(str(exc)) from None
    if recipe.get("needImage") is False:
        warnings.append("needImage=false：模型认为没有可替换的商品，出片时不传产品图")
    write_json(existing, recipe)
    write_json(wd / "vlm.json", answer)
    out({"recipe": str(existing), "id": recipe["id"], "name": recipe["name"], "mode": recipe.get("mode", "chain"),
         "needImage": recipe["needImage"], "products": recipe.get("products") or [], "aspectRatio": recipe["aspectRatio"],
         "slots": recipe["slots"],
         "steps": [{"id": s["id"], "kind": s["kind"], "title": s["title"]} for s in recipe["steps"]], "warnings": warnings,
         "next": "检查配方没问题后跑 package，导出给执行 agent 的交付包。"})


def cmd_package(args) -> None:
    wd = Path(args.workdir).expanduser().resolve()
    recipe = read_json(wd / "recipe.json", "recipe.json（先 recipe）")
    out_root = Path(args.out).expanduser().resolve() if args.out else wd / "export"
    choices = json_arg(args.slots, "slots", None)
    units = [int(x) for x in str(args.units).split(",") if x.strip()] if args.units else None
    try:
        pkg = handoff.build_package(wd, recipe, out_root, args.name, args.desc or "", args.category, units, choices)
    except (rcp.TemplateError, ValueError, FileNotFoundError) as exc:
        raise Fail(str(exc)) from None
    tpl = pkg["template"]
    out({"package": pkg["folder"], "zip": pkg["zip"], "id": tpl["id"], "name": tpl["name"], "needImage": tpl["needImage"],
         "products": rcp.product_list(tpl),
         "slots": tpl["slots"], "units": len(tpl["replica"]["units"]), "duration": tpl["replica"]["duration"],
         "files": sorted(p.relative_to(pkg["folder"]).as_posix() for p in Path(pkg["folder"]).rglob("*") if p.is_file()),
         "next": "把 zip（或整个文件夹）交给执行 agent：它读 AGENT.md，按段先图生图再图生视频，最后按原片时长拼接。"})


def cmd_status(args) -> None:
    wd = Path(args.workdir).expanduser().resolve()
    analysis = read_json(analysis_path(wd), "analysis.json") if analysis_path(wd).is_file() else None
    recipe = read_json(wd / "recipe.json", "recipe.json") if (wd / "recipe.json").is_file() else None
    packages = []
    for path in sorted((wd / "export").glob("*/template.json")):
        try:
            tpl = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        zip_path = path.parent.with_suffix(".zip")
        packages.append({"id": tpl.get("id"), "name": tpl.get("name"), "folder": rel(wd, path.parent),
                         "zip": rel(wd, zip_path) if zip_path.is_file() else ""})
    out({
        "workdir": str(wd),
        "analysis": {"mode": analysis["mode"], "units": len(analysis["units"]), "detector": analysis.get("detector"),
                     "speech": bool(analysis.get("speech", {}).get("segments"))} if analysis else None,
        "recipe": {"id": recipe["id"], "name": recipe["name"], "slots": recipe["slots"], "needImage": recipe["needImage"]} if recipe else None,
        "packages": packages,
    })


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("ingest")
    p.add_argument("video")
    p.add_argument("workdir")
    p.add_argument("--name")
    p.add_argument("--category")
    p.add_argument("--transcript")
    p.add_argument("--max-units", type=int, default=plan.DEFAULT_MAX_UNITS)
    p.add_argument("--mode", choices=["auto", "units", "whole"], default="auto")
    p = sub.add_parser("speech")
    p.add_argument("workdir")
    p.add_argument("--transcript", required=True)
    p = sub.add_parser("recipe")
    p.add_argument("workdir")
    p.add_argument("--products", "--product-images", dest="products", type=int, choices=(1, 2, 3), default=None,
                   help="how many distinct products the package should expect (1-3); default: whatever the answer lists")
    p.add_argument("--product-labels", "--product-roles", dest="product_labels",
                   help="comma separated names for those products, e.g. 卸妆膏,化妆棉")
    p.add_argument("--vlm")
    p.add_argument("--vlm-json")
    p.add_argument("--name")
    p = sub.add_parser("package")
    p.add_argument("workdir")
    p.add_argument("--name")
    p.add_argument("--desc")
    p.add_argument("--category")
    p.add_argument("--units")
    p.add_argument("--slots")
    p.add_argument("--out")
    p = sub.add_parser("status")
    p.add_argument("workdir")
    args = ap.parse_args()
    handlers = {"doctor": cmd_doctor, "ingest": cmd_ingest, "speech": cmd_speech, "recipe": cmd_recipe,
                "package": cmd_package, "status": cmd_status}
    try:
        handlers[args.cmd](args)
    except (Fail, FileNotFoundError, media.MediaError, plan.ShotAnalysisError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
