"""Shots -> generation units, transcript -> per-unit speech."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from desklib import media, shots_lite

UNIT_MIN_S = 4.0            # shortest single video-model generation
UNIT_MAX_S = 15.0           # longest single generation
MULTI_SHOT_PREFER_S = 8.0   # a multi-shot unit should stay under this
MULTI_SHOT_MAX_S = 10.0     # hard cap for a multi-shot unit
MULTI_SHOT_MAX_SHOTS = 4    # more cuts in one generation get blurred into one shot or dropped
SAME_SCENE_DISTANCE = 0.5   # Bhattacharyya distance between last / first frame histograms below this = same scene
JOIN_TRANSITIONS = {"cut", "jump", "flash"}  # dissolves / fades / wipes always split units
DEFAULT_MAX_UNITS = 6


class ShotAnalysisError(RuntimeError):
    """Shot stage failed (decode error, missing tool, video too long). Callers can fall back to whole-video mode."""


# ---------------------------------------------------------------- detection


def detector_name() -> str:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return "lite"
    return "cv2"


def detect_shots(path: Path) -> tuple[dict, list[dict], str]:
    """(meta, shot rows with hist_first / hist_last, detector name)."""
    media.require_tools()
    try:
        meta = media.probe(path)
    except media.MediaError as exc:
        raise ShotAnalysisError(str(exc)) from exc
    name = detector_name()
    try:
        if name == "cv2":
            from desklib import shots_cv

            rows = shots_cv.detect_path(path, meta)
        else:
            rows = shots_lite.detect_path(path, meta)
    except ShotAnalysisError:
        raise
    except Exception as exc:
        raise ShotAnalysisError(f"参考片切镜失败：{exc}") from exc
    if not rows:
        raise ShotAnalysisError("参考片没有解出画面")
    if not meta.get("duration"):
        meta["duration"] = rows[-1]["end"]
    return meta, rows, name


def scene_distance(a, b) -> float:
    try:
        import cv2
        import numpy as np

        if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            return float(cv2.compareHist(a, b, cv2.HISTCMP_BHATTACHARYYA))
    except ImportError:
        pass
    return shots_lite.bhattacharyya(list(a), list(b))


def same_scene(prev: dict, cur: dict) -> bool:
    if prev.get("hist_last") is None or cur.get("hist_first") is None:
        return False
    return scene_distance(prev["hist_last"], cur["hist_first"]) < SAME_SCENE_DISTANCE


def _runs(rows: list[dict], same) -> list[list[dict]]:
    runs: list[list[dict]] = []
    for row in rows:
        if runs and row.get("transition_in") in JOIN_TRANSITIONS and same(runs[-1][-1], row):
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def _split_run(run: list[dict]) -> list[list[dict]]:
    """Fewest units first, then avoid multi-shot units over 8 s and units under 4 s ."""
    n = len(run)
    best = [0.0] + [math.inf] * n
    prev = [0] * (n + 1)
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            seg = run[i:j]
            dur = sum(r["duration"] for r in seg)
            if len(seg) > 1 and (dur > MULTI_SHOT_MAX_S or len(seg) > MULTI_SHOT_MAX_SHOTS):
                break
            cost = 1.0 + (0.4 if len(seg) > 1 and dur > MULTI_SHOT_PREFER_S else 0.0) + (0.2 if dur < UNIT_MIN_S else 0.0)
            if best[i] + cost < best[j]:
                best[j], prev[j] = best[i] + cost, i
    out, j = [], n
    while j > 0:
        out.append(run[prev[j]:j])
        j = prev[j]
    return out[::-1]


def _split_long_shot(row: dict) -> list[dict]:
    parts = int(math.ceil(row["duration"] / UNIT_MAX_S))
    if parts <= 1:
        return [row]
    step = row["duration"] / parts
    pieces = []
    for k in range(parts):
        start = row["start"] + step * k
        pieces.append({**row, "start": round(start, 3), "end": round(start + step, 3), "duration": round(step, 3),
                       "transition_in": row["transition_in"] if k == 0 else "split"})
    return pieces


def _unit(seg: list[dict]) -> dict:
    start, end = seg[0]["start"], seg[-1]["end"]
    duration = round(end - start, 3)
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": duration,
        "generate": int(min(UNIT_MAX_S, max(UNIT_MIN_S, math.ceil(duration)))),
        "transitionIn": seg[0].get("transition_in") or "cut",
        "shots": [
            {"start": round(r["start"] - start, 3), "end": round(r["end"] - start, 3), "transition": r.get("transition_in") or "cut"}
            for r in seg
        ],
    }


def plan_units(rows: list[dict], same=same_scene, limit: int = DEFAULT_MAX_UNITS) -> list[dict]:
    pieces = [piece for row in rows for piece in _split_long_shot(row)]
    segs = [seg for run in _runs(pieces, same) for seg in _split_run(run)]
    while len(segs) > limit:
        best = None
        for i in range(len(segs) - 1):
            dur = segs[i + 1][-1]["end"] - segs[i][0]["start"]
            if dur <= UNIT_MAX_S and (best is None or dur < best[1]):
                best = (i, dur)
        if best is None:
            raise ShotAnalysisError(f"参考片太长：按镜头复刻最多 {limit} 段，每段不超过 {UNIT_MAX_S:.0f} 秒")
        i = best[0]
        segs[i:i + 2] = [segs[i] + segs[i + 1]]
    units = [_unit(seg) for seg in segs]
    for k, unit in enumerate(units, 1):
        unit["index"] = k
    return units


TRANSITION_SKIP_S = 0.05
FRAME_MAX_DELAY_S = 1.5


def first_frame_time(unit: dict, rows: list[dict]) -> float:
    """Where to grab a unit's first frame: after the transition that opens it, so no blend frame ends up
    as the composition reference. Clamped to half the unit, so a very long dissolve cannot walk into the
    next shot (in that case the transition covers most of the unit anyway)."""
    first = next((r for r in rows if abs(float(r["start"]) - float(unit["start"])) <= 0.05), None)
    delay = 0.0
    if first:
        delay = max(0.0, float(first.get("transition_end") or first["start"]) - float(first["start"]))
    t = float(unit["start"]) + delay + TRANSITION_SKIP_S
    t = min(t, float(unit["start"]) + max(0.1, min(FRAME_MAX_DELAY_S, float(unit["duration"]) * 0.5)))
    return round(t, 3)


def public_shots(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k not in ("hist_first", "hist_last")} for row in rows]


# ---------------------------------------------------------------- speech


def _clock(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def parse_transcript(path: Path) -> list[dict]:
    """[{start, end, text}] from .json (list or {segments|sentences}), .srt, .vtt or .tsv (start_ms, end_ms, text)."""
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    rows: list[dict] = []
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("segments") or data.get("sentences") or data.get("lines") or []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                rows.append({"start": float(item["start"]), "end": float(item["end"]), "text": str(item.get("text") or "").strip()})
            except (KeyError, TypeError, ValueError):
                continue
    elif suffix in (".srt", ".vtt"):
        for block in re.split(r"\n\s*\n", text.replace("\r", "")):
            lines = [line for line in block.split("\n") if line.strip()]
            for i, line in enumerate(lines):
                if "-->" in line:
                    a, b = line.split("-->")[:2]
                    body = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
                    if body:
                        rows.append({"start": _clock(a.split()[0]), "end": _clock(b.split()[0]), "text": body})
                    break
    elif suffix == ".tsv":
        for line in text.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].strip().isdigit() and parts[1].strip().isdigit() and parts[2].strip():
                rows.append({"start": int(parts[0]) / 1000, "end": int(parts[1]) / 1000, "text": parts[2].strip()})
    else:
        raise ValueError(f"不支持的口播格式：{suffix}（用 .json / .srt / .vtt / .tsv）")
    return [row for row in rows if row["text"] and row["end"] > row["start"]]


def assign_speech(segments: list[dict], units: list[dict]) -> list[str]:
    """Split recognised sentences across units by time overlap; a sentence spanning units is cut by word."""
    out: list[list[str]] = [[] for _ in units]
    for seg in segments:
        start, end = float(seg["start"]), float(seg["end"])
        words = seg["text"].split() or [seg["text"]]
        overlaps = [max(0.0, min(end, float(u["end"])) - max(start, float(u["start"]))) for u in units]
        total = sum(overlaps)
        if total <= 0:
            nearest = min(range(len(units)), key=lambda k: abs(float(units[k]["start"]) - start))
            out[nearest].append(seg["text"])
            continue
        taken = 0
        hits = [k for k, o in enumerate(overlaps) if o >= 0.25 * total] or [overlaps.index(max(overlaps))]
        total = sum(overlaps[k] for k in hits)
        for n, k in enumerate(hits):
            count = len(words) - taken if n == len(hits) - 1 else round(len(words) * overlaps[k] / total)
            piece = words[taken:taken + count]
            taken += len(piece)
            if piece:
                out[k].append(" ".join(piece))
    return [" ".join(parts).strip() for parts in out]


LANGUAGE_NAMES = {"en": "英语", "es": "西语", "zh": "中文"}
_SPANISH_HINTS = (" el ", " la ", " los ", " las ", " que ", " para ", " con ", " es ", " muy ", " y ", " una ", " este ")


def speech_language(segments: list[dict]) -> str:
    text = " ".join(seg["text"] for seg in segments)
    if not text.strip():
        return ""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if ch.isalpha() and ord(ch) < 0x250)
    if cjk >= latin:
        return "zh"
    lower = f" {text.lower()} "
    if any(ch in lower for ch in "ñ¿¡") or sum(lower.count(w) for w in _SPANISH_HINTS) >= 2:
        return "es"
    return "en"
