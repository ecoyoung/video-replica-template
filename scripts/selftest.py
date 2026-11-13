#!/usr/bin/env python3
"""Self-test for this skill (scripts/) (standard library only; ffmpeg cases are skipped when ffmpeg is missing).

  python3 scripts/selftest.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from desklib import flowhtml, handoff, media, plan, profile, shots_lite  # noqa: E402

try:  # optional fast path: only present when numpy + opencv are installed
    from desklib import shots_cv  # noqa: E402
    HAVE_CV = True
except ImportError:
    HAVE_CV = False
from desklib import recipe as rcp  # noqa: E402

FAILED = 0
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def check(name, fn):
    global FAILED
    try:
        fn()
        print(f"  ok   {name}")
    except Exception as exc:  # noqa: BLE001
        FAILED += 1
        print(f"  FAIL {name}\n       {type(exc).__name__}: {exc}")


def desk(*args) -> dict:
    proc = subprocess.run([sys.executable, "-I", str(SCRIPTS / "desk.py"), *map(str, args)], capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(f"desk.py {args[0]} printed no JSON: {proc.stdout[-300:]} {proc.stderr[-300:]}") from None


def row(i, start, end, transition="cut", scene="a"):
    return {"index": i, "start": start, "end": end, "duration": round(end - start, 3), "transition_in": transition, "scene": scene}


def same(prev, cur):
    return prev["scene"] == cur["scene"]


# ---------------------------------------------------------------- plan
print("\nplan_units (content-desk rules)")


def t_single_shots():
    units = plan.plan_units([row(1, 0, 3, "start", "a"), row(2, 3, 9, "cut", "b"), row(3, 9, 12, "cut", "c")], same)
    assert [(u["start"], u["end"], u["generate"]) for u in units] == [(0, 3, 4), (3, 9, 6), (9, 12, 4)], units


def t_same_scene_joins():
    units = plan.plan_units([row(1, 0, 2, "start"), row(2, 2, 4.5), row(3, 4.5, 7)], same)
    assert len(units) == 1 and len(units[0]["shots"]) == 3 and units[0]["generate"] == 7, units


def t_multi_shot_cap():
    rows = [row(i + 1, i * 2.0, i * 2.0 + 2, "cut" if i else "start") for i in range(6)]
    units = plan.plan_units(rows, same)
    assert all(len(u["shots"]) <= 4 and u["duration"] <= 10 for u in units), units


def t_dissolve_splits():
    units = plan.plan_units([row(1, 0, 2, "start"), row(2, 2, 4, "dissolve")], same)
    assert len(units) == 2, units


def t_long_shot_split():
    units = plan.plan_units([row(1, 0, 40, "start")], same)
    assert len(units) == 3 and all(u["duration"] <= 15 for u in units), units


def t_too_long():
    try:
        plan.plan_units([row(i + 1, i * 15.0, i * 15.0 + 15, "cut" if i else "start", str(i)) for i in range(7)], same)
    except plan.ShotAnalysisError:
        return
    raise AssertionError("7 x 15 s should be too long")


def t_merge_to_limit():
    rows = [row(i + 1, i * 2.0, i * 2.0 + 2, "cut" if i else "start", str(i)) for i in range(9)]
    units = plan.plan_units(rows, same)
    assert len(units) == 6 and units[-1]["end"] == 18, units


for name, fn in [("single shots keep their own units", t_single_shots), ("same-scene cuts become one multi-shot unit", t_same_scene_joins),
                 ("multi-shot unit stays <= 4 shots and <= 10 s", t_multi_shot_cap), ("dissolve always splits", t_dissolve_splits),
                 ("a 40 s take is split into <= 15 s pieces", t_long_shot_split), ("more than 6 x 15 s is refused", t_too_long),
                 ("too many units merge the shortest neighbours", t_merge_to_limit)]:
    check(name, fn)

print("\nspeech")


def t_assign():
    units = [{"start": 0, "end": 3}, {"start": 3, "end": 6}]
    out = plan.assign_speech([{"start": 1, "end": 5, "text": "one two three four"}], units)
    assert out == ["one two", "three four"], out


def t_language():
    assert plan.speech_language([{"text": "este es muy bueno para la casa"}]) == "es"
    assert plan.speech_language([{"text": "这个搅拌机太好用了"}]) == "zh"
    assert plan.speech_language([{"text": "this is great"}]) == "en"
    assert plan.speech_language([]) == ""


def t_srt():
    f = Path(tempfile.mkdtemp()) / "a.srt"
    f.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello <i>there</i>\n\n2\n00:00:02,500 --> 00:00:03,000\nBye\n", encoding="utf-8")
    rows = plan.parse_transcript(f)
    assert rows == [{"start": 0.5, "end": 2.0, "text": "Hello there"}, {"start": 2.5, "end": 3.0, "text": "Bye"}], rows


for name, fn in [("a sentence across units is split by overlap", t_assign), ("language detection", t_language), ("srt parsing", t_srt)]:
    check(name, fn)

print("\nrecipe")
UNITS = [{"index": 1, "start": 0, "end": 3, "duration": 3, "generate": 4, "transitionIn": "start", "shots": [{"start": 0, "end": 3, "transition": "start"}]},
         {"index": 2, "start": 3, "end": 8, "duration": 5, "generate": 5, "transitionIn": "cut", "shots": [{"start": 0, "end": 5, "transition": "cut"}]}]
ANSWER = {"desc": "d", "needImage": True, "aspectRatio": "9:16", "product": {"name": "Ninja blender", "aliases": ["Ninja"]},
          "slots": [{"title": "食材", "default": "banana、spinach"}, {"title": "产品", "default": "Ninja"}, {"title": "道具", "default": "Ninja cup、spoon"}],
          "voice": "女声", "units": [{"title": "开场", "keyframe": "近景 banana 在台面", "video": "手拿起 banana", "line": "banana time"},
                                    {"title": "收尾", "keyframe": "spoon 搅拌", "video": "固定", "line": ""}]}


def build():
    seen = rcp.slotify_fallback(rcp.drop_product_slots(rcp.normalize_units_answer(ANSWER, UNITS)))
    return rcp.build_recipe("draft-x", "T", "source/a.mp4", {"duration": 8, "aspectRatio": "9:16"}, UNITS,
                            ["frames/u1-first.jpg", "frames/u2-first.jpg"], seen)


def t_count():
    try:
        rcp.normalize_units_answer({**ANSWER, "units": ANSWER["units"][:1]}, UNITS)
    except ValueError as exc:
        assert "应为 2 段" in str(exc)
        return
    raise AssertionError("unit count mismatch must fail")


def t_product_slots():
    seen = rcp.drop_product_slots(rcp.normalize_units_answer(ANSWER, UNITS))
    titles = {s["title"]: s.get("default") for s in seen["slots"]}
    assert "产品" not in titles and titles["道具"] == "spoon", titles


def t_build():
    r = build()
    assert [s["id"] for s in r["steps"]] == ["u1-frame", "u1-video", "u2-frame", "u2-video", "assemble"]
    assert r["steps"][0]["prompt"].startswith(rcp.KEYFRAME_HEAD + rcp.KEYFRAME_PRODUCT)
    assert "【食材】" in r["steps"][0]["prompt"] and "banana" not in r["steps"][0]["prompt"]
    assert "口播（女声）：「banana time」" in r["steps"][1]["prompt"]
    assert r["steps"][1]["duration"] == 4 and r["steps"][1]["targetDuration"] == 3
    assert [s["title"] for s in r["slots"]] == ["食材", "道具"]
    assert r["replica"]["units"][0]["refUrl"] == "frames/u1-first.jpg"


def t_stitch():
    slots = [{"key": "s1", "title": "食材", "default": "banana"}]
    assert rcp.stitch("放入【食材】和【s1】", {"s1": "草莓"}, slots) == "放入草莓和草莓"
    assert rcp.stitch("放入【】", {}, slots) == "放入banana"
    assert rcp.stitch("【未知】", {}, slots) == "【未知】"


def t_leftover():
    left = rcp.leftover_items(["add oat milk and blend"], [{"title": "食材", "old": "banana、oat milk", "new": "mango"}])
    assert left == ["oat milk"], left


def t_template():
    r = build()
    job = {"id": "j1", "slots": {"s1": "草莓"}, "resultUrl": "jobs/j1/remake.mp4", "steps": [{"kind": "i2i", "outputUrl": "jobs/j1/u1-frame.png"}]}
    tpl = rcp.build_template(r, job, "模板A", category="厨房", slot_choices=[{"key": "s2", "keep": False}, {"key": "s1", "title": "水果"}], keep_units=[2])
    assert tpl["origin"] == "template" and tpl["id"].startswith("tpl-") and tpl["category"] == "厨房"
    assert tpl["slots"] == [{"key": "s1", "title": "水果", "default": "草莓"}], tpl["slots"]
    assert [s["id"] for s in tpl["steps"]] == ["u2-frame", "u2-video", "assemble"]
    assert "【道具】" not in tpl["steps"][0]["prompt"] and "spoon" in tpl["steps"][0]["prompt"]
    assert tpl["replica"]["duration"] == 5 and tpl["sampleUrl"] == "jobs/j1/remake.mp4" and tpl["coverUrl"] == "jobs/j1/u1-frame.png"


def t_whole():
    seen = rcp.normalize_recipe({"needImage": True, "shots": [{"kind": "t2i", "title": "a", "prompt": "p"}, {"kind": "i2v", "title": "b", "prompt": "q", "duration": 6}]})
    r = rcp.build_whole_recipe("draft-w", "W", "source/a.mp4", "", seen)
    assert [(s["kind"], s.get("imageFrom")) for s in r["steps"]] == [("i2i", "upload"), ("i2v", "prev")], r["steps"]


for name, fn in [("unit count must match the shot table", t_count), ("product never stays a slot", t_product_slots),
                 ("recipe steps, keyframe head, slot markers, voice line", t_build), ("slot stitching", t_stitch),
                 ("leftover original values are reported", t_leftover), ("save as template: keep/rename/drop slots, keep units", t_template),
                 ("whole-video fallback chain", t_whole)]:
    check(name, fn)

print("\nhandoff package")


def t_package():
    wd = Path(tempfile.mkdtemp())
    (wd / "frames").mkdir()
    for k in (1, 2):
        (wd / "frames" / f"u{k}-first.jpg").write_bytes(b"jpg")
    (wd / "source").mkdir()
    (wd / "source" / "a.mp4").write_bytes(b"mp4")
    r = build()
    r["replica"].update({"width": 1080, "height": 1920, "voice": "女声", "language": "en"})
    pkg = handoff.build_package(wd, r, wd / "export", "奶昔模板", "d", "厨房", None, [{"key": "s1", "title": "水果"}])
    folder = Path(pkg["folder"])
    assert folder.name.isascii() and folder.name.startswith("tpl-"), folder.name
    names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert names == ["AGENT.md", "flow.html", "frames/u1-first.jpg", "frames/u2-first.jpg", "template.json"], names
    tpl = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    assert tpl["origin"] == "template" and tpl["name"] == "奶昔模板" and tpl["previewM3u8"] == ""
    assert tpl["steps"][0]["refUrl"] == "frames/u1-first.jpg" and tpl["coverUrl"] == "frames/u1-first.jpg"
    assert "【水果】" in tpl["steps"][0]["prompt"] and tpl["slots"][0]["title"] == "水果"
    h = tpl["handoff"]
    assert h["schema"] == "video-replica-handoff" and h["inputs"]["productImage"] == "required" and h["diagram"] == "flow.html"
    assert h["units"][0]["frame"]["references"] == ["frames/u1-first.jpg", "<productImage>"]
    assert h["units"][0]["video"]["duration"] == 4 and h["units"][0]["targetDuration"] == 3
    assert h["output"] == {"width": 1080, "height": 1920, "aspectRatio": "9:16", "duration": 8, "fpsMax": 30}
    md = (folder / "AGENT.md").read_text(encoding="utf-8")
    for needle in ("产品图：**必填**", "| s1 | 水果 |", "### 段 1 · 开场", "frames/u1-first.jpg", "口播（女声）：「banana time」", "## 5. 拼接", "1080×1920"):
        assert needle in md, needle
    import zipfile
    with zipfile.ZipFile(pkg["zip"]) as z:
        assert sorted(n for n in z.namelist() if not n.endswith("/")) == sorted(f"{folder.name}/{n}" for n in names)
    page = (folder / "flow.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
    kinds = [n["kind"] for n in data["nodes"]]
    assert kinds == ["image", "slot", "slot", "i2i", "i2v", "i2i", "i2v", "assemble", "out"], kinds
    tones = sorted({e["tone"] for e in data["edges"]})
    assert tones == ["feed", "image", "out", "slot"], tones
    assert {"from": "in-image", "to": "u1-frame", "tone": "image"} in data["edges"]
    assert {"from": "in-slot-s1", "to": "u1-frame", "tone": "slot"} in data["edges"], "slot wired to the step that uses it"
    assert not any(e["from"] == "in-slot-s2" and e["to"] == "u1-video" for e in data["edges"]), "unused slot not wired"
    assert "<title>奶昔模板 · 复刻流程</title>" in page and "frames/u1-first.jpg" in page and "http" not in page.split("<script>")[0].split("<style>")[0]
    pkg2 = handoff.build_package(wd, r, wd / "export", None, "", None, [2], None)
    tpl2 = json.loads((Path(pkg2["folder"]) / "template.json").read_text(encoding="utf-8"))
    assert [u["index"] for u in tpl2["replica"]["units"]] == [2] and tpl2["coverUrl"] == "frames/u2-first.jpg"
    assert '"u1-frame"' not in (Path(pkg2["folder"]) / "flow.html").read_text(encoding="utf-8")
    try:
        handoff.build_package(wd, {**r, "mode": "chain"}, wd / "export")
        raise AssertionError("whole-video recipes have no units to hand off")
    except ValueError:
        pass


check("package: ASCII folder, relative paths, handoff block, AGENT.md, flow.html graph, zip, unit subset", t_package)

if HAVE_FFMPEG:
    print("\nffmpeg end-to-end")
    tmp = Path(tempfile.mkdtemp())
    video = tmp / "ref.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=360x640:d=3:r=30", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=3:r=30",
                    "-f", "lavfi", "-i", "color=c=yellow:s=360x640:d=4:r=30", "-f", "lavfi", "-i", "sine=f=440:d=10",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p[v]", "-map", "[v]", "-map", "3:a", "-shortest",
                    "-c:v", "libx264", "-c:a", "aac", str(video)], check=True)

    def t_lite():
        shots = shots_lite.detect_path(video, media.probe(video))
        assert [s["start"] for s in shots] == [0.0, 3.0, 6.0], [(s["start"], s["transition_in"]) for s in shots]

    def t_e2e():
        wd = tmp / "wd"
        srt = tmp / "t.srt"
        srt.write_text("1\n00:00:00,300 --> 00:00:02,500\nhello banana\n\n2\n00:00:06,500 --> 00:00:09,000\nbuy now\n", encoding="utf-8")
        d = desk("ingest", video, wd, "--name", "T", "--transcript", srt)
        assert d["ok"] and d["mode"] == "units" and len(d["units"]) == 3, d
        assert d["units"][0]["speech"] == "hello banana" and d["units"][2]["speech"] == "buy now"
        answer = {"needImage": True, "slots": [{"title": "食材", "default": "banana"}],
                  "units": [{"title": f"u{i}", "keyframe": "banana 在台面" if i == 1 else "k", "video": "v", "line": "hi banana" if i == 1 else ""} for i in (1, 2, 3)]}
        (wd / "vlm.json").write_text(json.dumps(answer), encoding="utf-8")
        d = desk("recipe", wd)
        assert d["ok"] and [s["title"] for s in d["slots"]] == ["食材"], d
        d = desk("package", wd, "--name", "模板")
        assert d["ok"] and Path(d["zip"]).is_file() and "flow.html" in d["files"] and "reference.mp4" not in d["files"], d
        s = desk("status", wd)
        assert s["ok"] and s["recipe"]["slots"][0]["title"] == "食材" and len(s["packages"]) == 1, s

    if HAVE_CV:
        def t_cv_cuts():
            shots = shots_cv.detect_path(video, media.probe(video))
            assert [s["start"] for s in shots] == [0.0, 3.0, 6.0], [(s["start"], s["transition_in"]) for s in shots]

        def _two_clip(name, xfade, right="testsrc2=s=320x568:d=2:r=30"):
            left = "smptebars=s=320x568:d=2:r=30"
            out = tmp / f"{name}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", left, "-f", "lavfi", "-i", right,
                            "-filter_complex", f"[0][1]xfade=transition={xfade}:duration=0.8:offset=1.1[v]",
                            "-map", "[v]", "-t", "2.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
            return out

        def t_cv_dissolve():
            shots = shots_cv.detect_path(_two_clip("diss", "fade"), media.probe(tmp / "diss.mp4"))
            assert len(shots) == 2 and shots[1]["transition_in"] == "dissolve", [(s["start"], s["transition_in"]) for s in shots]

        def t_cv_wipe():
            shots = shots_cv.detect_path(_two_clip("wipe", "wipeleft"), media.probe(tmp / "wipe.mp4"))
            assert len(shots) == 2 and shots[1]["transition_in"] == "wipe", [(s["start"], s["transition_in"]) for s in shots]

        def t_cv_fade():
            out = tmp / "fade.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "smptebars=s=320x568:d=2:r=30",
                            "-f", "lavfi", "-i", "color=c=0x2b6cb0:s=320x568:d=2:r=30", "-filter_complex",
                            "[0]fade=t=out:st=1.2:d=0.8[a];[1]fade=t=in:st=0:d=0.8[b];[a][b]concat=n=2:v=1:a=0[v]",
                            "-map", "[v]", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
            shots = shots_cv.detect_path(out, media.probe(out))
            assert len(shots) == 2 and shots[1]["transition_in"] == "fade", [(s["start"], s["transition_in"]) for s in shots]

        check("cv2 detector finds the two hard cuts", t_cv_cuts)
        check("cv2 detector sees a dissolve as one boundary", t_cv_dissolve)
        check("cv2 detector sees a fade through black", t_cv_fade)

        def t_cv_pan():
            out = tmp / "pan.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1600x960:d=3:r=30",
                            "-vf", "crop=540:960:x='min(1050,t*260)':y=0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            str(out)], check=True)
            shots = shots_cv.detect_path(out, media.probe(out))
            assert len(shots) == 1, [(s["start"], s["transition_in"]) for s in shots]

        def t_cv_mg_cards():
            out = tmp / "cards.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x223344:s=320x568:d=1.2",
                            "-f", "lavfi", "-i", "color=c=0x884422:s=320x568:d=1.2", "-f", "lavfi", "-i", "color=c=0x2f6f3f:s=320x568:d=1.2",
                            "-filter_complex",
                            "[0]drawbox=x=40:y=80:w=240:h=180:color=white:t=fill[a];"
                            "[1]drawbox=x=40:y=260:w=240:h=180:color=yellow:t=fill[b];"
                            "[2]drawbox=x=40:y=380:w=240:h=120:color=cyan:t=fill[c];"
                            "[a][b][c]concat=n=3:v=1:a=0,format=yuv420p[v]", "-map", "[v]", "-r", "30",
                            "-c:v", "libx264", str(out)], check=True)
            shots = shots_cv.detect_path(out, media.probe(out))
            assert len(shots) == 3, [(s["start"], s["transition_in"]) for s in shots]

        def t_cv_flash():
            out = tmp / "flash.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x568:d=1.2:r=30",
                            "-f", "lavfi", "-i", "color=c=white:s=320x568:d=0.034:r=30",
                            "-f", "lavfi", "-i", "smptebars=s=320x568:d=1.2:r=30", "-filter_complex",
                            "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p[v]", "-map", "[v]", "-c:v", "libx264", str(out)],
                           check=True)
            shots = shots_cv.detect_path(out, media.probe(out))
            assert len(shots) == 2 and shots[1]["transition_in"] == "flash", [(s["start"], s["transition_in"]) for s in shots]

        check("cv2 detector sees a wipe as one boundary", t_cv_wipe)
        check("cv2 detector labels a one-frame flash", t_cv_flash)
        check("cv2 detector does not cut on a pan", t_cv_pan)
        check("cv2 detector splits motion-graphics cards", t_cv_mg_cards)

    check("lite detector finds the two hard cuts", t_lite)

    def t_first_frame():
        rows = [{"start": 0.0, "end": 1.0, "transition_in": "start", "transition_start": 0.0, "transition_end": 0.0},
                {"start": 1.0, "end": 2.6, "transition_in": "dissolve", "transition_start": 0.9, "transition_end": 1.7},
                {"start": 2.6, "end": 5.0, "transition_in": "fade", "transition_start": 2.2, "transition_end": 3.0}]
        opener = {"index": 1, "start": 1.0, "end": 2.6, "duration": 1.6}
        assert plan.first_frame_time(opener, rows) == 1.75, plan.first_frame_time(opener, rows)
        first = {"index": 0, "start": 0.0, "end": 1.0, "duration": 1.0}
        assert plan.first_frame_time(first, rows) == 0.05, plan.first_frame_time(first, rows)
        long_fade = {"index": 2, "start": 2.6, "end": 5.0, "duration": 2.4}
        # a slow dissolve must not push the reference frame past a quarter of the unit
        assert plan.first_frame_time(long_fade, rows) <= 2.6 + 0.6, plan.first_frame_time(long_fade, rows)

    check("first frame lands after the opening transition", t_first_frame)

    def t_language():
        cases = (("这是中文台词，一罐就够了", "zh"), ("これは日本語のナレーションです", "ja"),
                 ("This balm melts makeup fast", "en"), ("Esta crema desmaquilla muy rápido", "es"))
        for text, want in cases:
            got = plan.speech_language([{"text": text}])
            assert got == want, (text, got, want)
        assert plan.LANGUAGE_NAMES.get("ja"), plan.LANGUAGE_NAMES

    check("speech language: zh / ja / en / es", t_language)

    if HAVE_FFMPEG:
        def t_look():
            out = tmp / "look.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x203040:s=320x568:d=1.0:r=30",
                            "-f", "lavfi", "-i", "color=c=0xd8c8a8:s=320x568:d=1.0:r=30", "-filter_complex",
                            "[0]drawbox=x=60:y=120:w=200:h=200:color=0x4060a0:t=fill[a];"
                            "[1]drawbox=x=60:y=120:w=200:h=200:color=0xf0e0c0:t=fill[b];"
                            "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]", "-map", "[v]", "-r", "30",
                            "-c:v", "libx264", str(out)], check=True)
            meta = media.probe(out)
            rows = shots_lite.detect_path(out, meta)
            profiles = profile.shot_profiles(out, meta, rows)
            assert len(profiles) == len(rows) and all(p.get("brightness") is not None for p in profiles), profiles
            dark, light = profiles[0], profiles[-1]
            assert light["brightness"] - dark["brightness"] > 20, (dark["brightness"], light["brightness"])
            assert light["warmth"] > dark["warmth"], (dark["warmth"], light["warmth"])
            assert all(p["motion"] in {"static", "pan", "zoom", "handheld", "slow", "moving"} for p in profiles), profiles
            text = profile.look_text(light)
            assert text and "固定机位" in text, text
            unit = {"index": 1, "start": rows[0]["start"], "end": rows[0]["end"], "duration": rows[0]["duration"]}
            assert profile.unit_look(profiles, rows, unit).get("brightness") == dark["brightness"]

        check("look profile: brightness, warmth, motion, text", t_look)
    check("ingest -> speech -> recipe -> package -> status", t_e2e)
else:
    print("\n(ffmpeg missing: end-to-end cases skipped)")

print(f"\nFAILED {FAILED}" if FAILED else "\nall checks passed")
sys.exit(1 if FAILED else 0)
