"""Shot boundary detection in pure Python (no numpy / OpenCV).

Hard cuts, jump cuts, flash transitions, fades through black/white, plus the flash / glitch filters and the
minimum-shot-length rules. Dissolves, wipes and motion-graphics beats are editor transitions that are NOT
detected here: a dissolve reads as slow continuous change, so the shots on both sides of it stay one shot.

Frames are read twice through an ffmpeg rawvideo pipe: tiny gray thumbs for pixel statistics and smaller
RGB thumbs for the HSV histogram ([16, 4, 4] bins, OpenCV 8-bit HSV convention, L1-normalised; the
Bhattacharyya distance between two of these equals cv2.compareHist(..., HISTCMP_BHATTACHARYYA)).
"""

from __future__ import annotations

import math
from pathlib import Path

from desklib import media

THUMB_W = 96          # gray thumbs for pixel statistics
HIST_W = 32           # RGB thumbs for the per-frame histogram distance (cut score)
SCENE_HIST_W = 96     # first / last frame of each shot, for the same-scene check
JUMP_MIN_CHANGED, JUMP_RATIO, JUMP_QUIET, JUMP_MIN_RETURN = 0.17, 2.5, 2, 0.7
SHORT_SHOT_S = 0.12
SQ = [i * i for i in range(256)]


_BIN_CACHE: dict[int, int] = {}


def _bin(r: int, g: int, b: int) -> int:
    """OpenCV 8-bit HSV (H 0..180, S/V 0..255, rounded) -> calcHist bin of [16, 4, 4] over [0,180)x[0,256)x[0,256).
    -1 when H rounds to 180 (outside calcHist's half-open range, so OpenCV does not count it either)."""
    v = max(r, g, b)
    diff = v - min(r, g, b)
    s = int(round(diff * 255.0 / v)) if v else 0
    if diff == 0:
        h = 0.0
    elif v == r:
        h = 60.0 * (g - b) / diff
    elif v == g:
        h = 120.0 + 60.0 * (b - r) / diff
    else:
        h = 240.0 + 60.0 * (r - g) / diff
    if h < 0:
        h += 360.0
    h8 = int(round(h / 2))
    if h8 >= 180:
        return -1
    return (h8 * 16 // 180) * 16 + (s * 4 >> 8) * 4 + (v * 4 >> 8)


def hsv_hist(rgb: bytes) -> list[float]:
    hist = [0.0] * 256
    cache = _BIN_CACHE
    for k in range(0, len(rgb), 3):
        key = (rgb[k] << 16) | (rgb[k + 1] << 8) | rgb[k + 2]
        idx = cache.get(key)
        if idx is None:
            idx = cache[key] = _bin(rgb[k], rgb[k + 1], rgb[k + 2])
        if idx >= 0:
            hist[idx] += 1
    total = sum(hist) or 1.0
    return [x / total for x in hist]


def bhattacharyya(a, b) -> float:
    s1, s2 = float(sum(a)), float(sum(b))
    if s1 <= 0 or s2 <= 0:
        return 1.0
    acc = sum(math.sqrt(float(x) * float(y)) for x, y in zip(a, b))
    return math.sqrt(max(0.0, 1.0 - acc / math.sqrt(s1 * s2)))


def _std(g: bytes) -> tuple[float, float]:
    n = len(g)
    mean = sum(g) / n
    var = sum(map(SQ.__getitem__, g)) / n - mean * mean
    return mean, math.sqrt(max(0.0, var))


def _mad(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _local_median(x: list[float], radius: int) -> list[float]:
    n = len(x)
    out = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        window = (x[lo:i] + x[i + 1:hi]) if hi - lo > 1 else x[lo:hi]
        out.append(_median(window))
    return out


class Track:
    def __init__(self, fps: float):
        self.fps = fps
        self.hist_dist: list[float] = []
        self.pix_diff: list[float] = []
        self.changed: list[float] = []
        self.luma: list[float] = []
        self.std: list[float] = []
        self.thumbs: list[bytes] = []
        self.hists: list[list[float]] = []
        self.times: list[float] | None = None

    @property
    def n(self) -> int:
        return len(self.luma)


def build_track(path: Path, meta: dict) -> Track:
    fps = float(meta.get("fps") or 0) or 30.0
    track = Track(fps)
    prev = None
    prev_std = 0.0
    for g in media.iter_raw(path, meta, THUMB_W, "gray"):
        mean, std = _std(g)
        if prev is None:
            track.pix_diff.append(0.0)
            track.changed.append(0.0)
        else:
            thr = max(6.0, 0.35 * min(std, prev_std))
            total = 0
            moved = 0
            for x, y in zip(g, prev):
                d = x - y if x > y else y - x
                total += d
                if d > thr:
                    moved += 1
            track.pix_diff.append(total / len(g) / 255.0)
            track.changed.append(moved / len(g))
        track.luma.append(mean)
        track.std.append(std)
        track.thumbs.append(g)
        prev, prev_std = g, std
    prev_h = None
    for rgb in media.iter_raw(path, meta, HIST_W, "rgb24"):
        h = hsv_hist(rgb)
        track.hist_dist.append(0.0 if prev_h is None else bhattacharyya(prev_h, h))
        track.hists.append(h)
        prev_h = h
    # both passes decode every frame; guard against an off-by-one from a damaged tail
    n = min(len(track.luma), len(track.hists))
    for name in ("pix_diff", "changed", "luma", "std", "thumbs", "hists", "hist_dist"):
        setattr(track, name, getattr(track, name)[:n])
    track.times = media.frame_times(path, n, fps)
    return track


def boundary_hists(path: Path, meta: dict, frames: set[int]) -> dict[int, list[float]]:
    """Histograms of selected frames at the thumb width (one extra decode pass)."""
    out: dict[int, list[float]] = {}
    for i, rgb in enumerate(media.iter_raw(path, meta, SCENE_HIST_W, "rgb24")):
        if i in frames:
            out[i] = hsv_hist(rgb)
            if len(out) == len(frames):
                break
    return out


def detect_path(path: Path, meta: dict) -> list[dict]:
    track = build_track(path, meta)
    shots = detect(track)
    needed = {s["start_frame"] for s in shots} | {max(s["start_frame"], s["end_frame"] - 1) for s in shots}
    exact = boundary_hists(path, meta, needed)
    for shot in shots:
        last = max(shot["start_frame"], shot["end_frame"] - 1)
        shot["hist_first"] = exact.get(shot["start_frame"], shot["hist_first"])
        shot["hist_last"] = exact.get(last, shot["hist_last"])
    return shots


def _t(track: Track, frame: int) -> float:
    times = track.times
    if times is None:
        return frame / track.fps
    if frame < len(times):
        return float(times[frame])
    step = _median([b - a for a, b in zip(times, times[1:])]) if len(times) > 1 else 1 / track.fps
    return float(times[-1]) + step * (frame - len(times) + 1)


def _returns(track: Track, i: int, span: int = 4) -> float:
    thumbs, n = track.thumbs, track.n
    if i < 1 or i + 1 >= n:
        return 1.0
    first = _mad(thumbs[i], thumbs[i - 1]) + 1e-6
    best = float("inf")
    for j in range(max(0, i - 3), i):
        jump = _mad(thumbs[i], thumbs[j]) + 1e-6
        if jump < 0.2 * first:
            continue
        later = min(_mad(thumbs[k], thumbs[j]) for k in range(i + 1, min(n, i + 1 + span)))
        best = min(best, later / jump)
    return best if best != float("inf") else 1.0


def _brief_flash(track: Track, i: int) -> bool:
    pre = min(track.luma[max(0, i - 3):i])
    rise = track.luma[i] - pre
    if rise <= 5.0 or i + 2 >= track.n:
        return False
    return track.luma[i + 2] - pre < 0.5 * rise and _returns(track, i) < 0.6


def _back_to_before(track: Track, i: int) -> bool:
    if i < 3:
        return False
    first = _mad(track.thumbs[i], track.thumbs[i - 1]) + 1e-6
    return min(_mad(track.thumbs[i], track.thumbs[j]) for j in (i - 3, i - 2)) < 0.3 * first


def _jump_cut(track: Track, i: int) -> bool:
    n = track.n
    if i < 4 or i + 4 > n:
        return False
    c, d = track.changed, track.pix_diff
    if c[i] < JUMP_MIN_CHANGED:
        return False
    side_c = max(_median(c[max(0, i - 6):i]), _median(c[i + 1:i + 7]))
    side_d = max(_median(d[max(0, i - 6):i]), _median(d[i + 1:i + 7]))
    if c[i] < JUMP_RATIO * (side_c + 0.02) or d[i] < 2.0 * (side_d + 0.004):
        return False
    near = c[i - JUMP_QUIET:i] + c[i + 1:i + 1 + JUMP_QUIET]
    if max(near) > 0.5 * c[i]:
        return False
    return _returns(track, i) >= JUMP_MIN_RETURN


def _flash(luma: list[float], start: int, end: int, fps: float) -> float:
    if start < 1 or end - start < 3:
        return 0.0
    before = _median(luma[max(0, start - 3):start])
    tail = luma[start + 1:end]
    settle = _median(tail[-max(1, min(end - start - 1, int(fps))):])
    jump = luma[start] - max(before, settle)
    if jump < 40:
        return 0.0
    k = start
    while k + 1 < end and luma[k + 1] - settle > 0.2 * jump:
        k += 1
    if k == start or k - start > fps * 1.5:
        return 0.0
    return (k + 1 - start) / fps


def _clean_cut(boundaries: dict, b: int) -> bool:
    kind, strength = boundaries[b]
    return kind in ("cut", "jump") and strength >= 0.35


def detect(track: Track, min_shot_s: float = 0.35, cut_threshold: float = 0.30) -> list[dict]:
    n = track.n
    if n == 0:
        return []
    fps = track.fps
    score = [0.6 * h + 0.4 * min(1.0, max(0.0, p * 4.0)) for h, p in zip(track.hist_dist, track.pix_diff)]
    score[0] = 0.0
    radius = max(4, int(round(fps * 0.5)))
    base = _local_median(score, radius)
    chg = track.changed
    chg_base = _local_median(chg, radius)
    boundaries: dict[int, tuple[str, float]] = {}
    spans: dict[int, tuple[int, int]] = {}

    for i in range(1, n):
        s = score[i]
        strong = s > cut_threshold and s > base[i] * 2.5 + 0.06 and chg[i] > 0.25
        relative = chg[i] > 0.45 and chg[i] > chg_base[i] * 3 + 0.2 and s > base[i] * 4 + 0.03
        still_swap = (s > cut_threshold and track.hist_dist[i] > 0.25 and chg[i] > 0.18 and chg_base[i] < 0.01
                      and s > base[i] * 5 + 0.1 and i >= 3 and max(track.pix_diff[i - 3:i]) < 1e-3)
        jump = _jump_cut(track, i)
        if strong or relative or still_swap or jump:
            if _brief_flash(track, i) or _back_to_before(track, i):
                continue
            same_take = jump and track.hist_dist[i] < 0.4 and chg[i] < 0.5
            boundaries[i] = ("jump" if same_take else "cut", float(max(s, chg[i])))

    # fades through black / white
    lo_l = min(10.0, _median(track.luma) * 0.35)
    dark = [(lum < lo_l) or (lum > 248) for lum in track.luma]
    i = 0
    while i < n:
        if dark[i]:
            j = i
            while j < n and dark[j]:
                j += 1
            flat = min(track.std[k] for k in range(i, j)) < 4.0
            if 0 < i and j < n and (j - i) <= fps * 2.5 and flat:
                if not any(abs(j - b) < radius for b in boundaries):
                    boundaries[j] = ("fade", 0.5)
                    spans[j] = (i, j)
            i = j
        else:
            i += 1

    min_len = max(1, int(round(min_shot_s * fps)))
    short_len = max(1, int(round(SHORT_SHOT_S * fps)))
    kept: list[int] = []
    for b in sorted(boundaries):
        prev = kept[-1] if kept else 0
        gap = b - prev
        held = max(chg[prev + 1:b], default=0.0) < 0.3
        if gap >= min_len or (gap >= short_len and _clean_cut(boundaries, b) and (not kept or _clean_cut(boundaries, prev)) and held):
            kept.append(b)
        elif kept and boundaries[b][1] > boundaries[prev][1] and b - (kept[-2] if len(kept) > 1 else 0) >= short_len:
            kept[-1] = b
    if kept and n - kept[-1] < min_len:
        kept.pop()

    shots = []
    start, trans, bscore = 0, "start", 0.0
    span_start = span_end = 0
    for b in kept + [n]:
        transition = trans
        if transition == "cut":
            flash = _flash(track.luma, start, b, fps)
            if flash:
                transition = "flash"
                span_start, span_end = start, min(b, start + int(round(flash * fps)))
        t0, t1 = _t(track, start), _t(track, b)
        shots.append({
            "index": len(shots) + 1,
            "start": round(t0, 3),
            "end": round(t1, 3),
            "duration": round(t1 - t0, 3),
            "start_frame": start,
            "end_frame": b,
            "transition_in": transition,
            "transition_start": round(_t(track, span_start), 3),
            "transition_end": round(_t(track, span_end), 3),
            "boundary_score": round(bscore, 3),
            "hist_first": track.hists[start],
            "hist_last": track.hists[max(start, b - 1)],
        })
        if b < n:
            trans, bscore = boundaries[b]
            span_start, span_end = spans.get(b, (b, b))
        start = b
    return shots
