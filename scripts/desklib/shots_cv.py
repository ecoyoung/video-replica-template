"""Shot boundary detection with numpy + OpenCV (optional, faster and more complete than shots_lite).

Per frame, on a thumbnail: an HSV histogram distance, the mean pixel difference and the share of touched
pixels. A cut has to stand out from the local activity level, so shake, focus pulls and titles popping in
do not fire on their own.

On top of the hard-cut family this detector also resolves editor transitions, which is the reason to
install numpy + opencv at all:

* dissolve — a frame in the middle of a cross-fade is a linear blend of the frames on both sides;
* wipe — the new shot eats the old one region by region, so some cells already look new, some still old;
* fade — the picture goes (nearly) uniform for a few frames between two shots.

Boundaries are collected as *events* (start frame … end frame) and resolved by strength, so one
transition yields exactly one boundary and a hard cut hiding inside a dissolve cannot survive as a
second one. The row schema is the same as shots_lite.detect_path, so plan can swap detectors freely.
"""

from __future__ import annotations

import sys

import numpy as np

try:  # only imported when plan.detector_name() reports the stack is available
    import cv2
except ImportError as exc:  # pragma: no cover - the caller guards this
    raise ImportError("shots_cv needs opencv-python and numpy") from exc

from desklib import media

THUMB_W = 160          # decode width for the per-frame statistics
CHECK_W = 80           # thumbs kept in memory for the blend / region checks
HIST_BINS = (12, 6, 6)
HIST_RANGES = [0, 180, 0, 256, 0, 256]
CUT_SCORE = 0.28       # absolute floor for a hard cut
MIN_SHOT_S = 0.40      # shorter shots are merged into a neighbour
SHORT_INSERT_S = 0.12  # ... unless both sides are clean hard cuts (a 3-frame insert in a fast edit)
JUMP_TOUCH = 0.16      # jump cut: share of the frame that has to change
SWAP_TOUCH = 0.15      # layout swap: same, for an on-screen card changing
BLOCK_GRID = (3, 4)    # coarse block grid: cheap region statistics for wipes and motion-graphics beats
WIPE_WINDOWS = (0.3, 0.6, 1.0, 1.6)  # window lengths tried for a gradual region-by-region transition
WIPE_MIN_TEXTURE = 4.0  # mean |Laplacian|: smooth gradients and flat cards are MG, not photographic wipes
FROZEN_TOUCH = 0.004   # share of changed pixels below which a frame counts as frozen
HOLD_MIN_S = 0.40      # shortest frozen hold that can be a motion-graphics beat


class Track:
    """Per-frame numbers plus the small thumbs the blend / region checks need."""

    def __init__(self, fps: float):
        self.fps = float(fps or 30.0)
        self.hist: list[np.ndarray] = []
        self.hist_dist: list[float] = []
        self.pix: list[float] = []
        self.touch: list[float] = []
        self.luma: list[float] = []
        self.std: list[float] = []
        self.thumb: list[np.ndarray] = []
        self.times: list[float] | None = None

    @property
    def n(self) -> int:
        return len(self.luma)

    def arrays(self) -> dict[str, np.ndarray]:
        return {k: np.asarray(getattr(self, k), dtype=np.float32)
                for k in ("hist_dist", "pix", "touch", "luma", "std")}


def _hist(rgb: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, HIST_BINS, HIST_RANGES)
    cv2.normalize(h, h, alpha=1.0, norm_type=cv2.NORM_L1)
    return h.reshape(-1).astype(np.float32)


def build_track(path, meta: dict) -> Track:
    track = Track(meta.get("fps") or 30.0)
    prev_gray = None
    prev_hist = None
    prev_std = 0.0
    for rgb_bytes in media.iter_raw(path, meta, THUMB_W, "rgb24"):
        flat = np.frombuffer(rgb_bytes, dtype=np.uint8)
        rows = max(1, flat.size // (THUMB_W * 3))
        rgb = flat[: THUMB_W * rows * 3].reshape(rows, THUMB_W, 3)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        small = cv2.resize(gray, (CHECK_W, max(1, round(rows * CHECK_W / THUMB_W))),
                           interpolation=cv2.INTER_AREA)
        track.thumb.append(small)
        hist = _hist(rgb)
        track.hist.append(hist)
        track.luma.append(float(gray.mean()))
        std = float(gray.std())
        track.std.append(std)
        if prev_gray is None:
            track.pix.append(0.0)
            track.touch.append(0.0)
            track.hist_dist.append(0.0)
        else:
            diff = cv2.absdiff(gray, prev_gray)
            track.pix.append(float(diff.mean()) / 255.0)
            track.touch.append(float((diff > max(8.0, 0.4 * min(std, prev_std))).mean()))
            track.hist_dist.append(float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)))
        prev_gray, prev_hist, prev_std = gray, hist, std
    if track.n:
        n = min(len(track.hist), track.n)
        for name in ("pix", "touch", "hist_dist", "luma", "std", "hist", "thumb"):
            setattr(track, name, getattr(track, name)[:n])
        track.times = media.frame_times(path, n, track.fps)
    return track


# ---------------------------------------------------------------- frame statistics


def _rolling_median(x: np.ndarray, radius: int) -> np.ndarray:
    """Median of each window with the sample itself left out: a cut must stand out from its neighbours."""
    n = x.size
    if n < 3 or radius < 1:
        return np.zeros_like(x)
    pad = np.pad(x, (radius, radius), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad, 2 * radius + 1).copy()
    windows[:, radius] = np.nan
    with np.errstate(all="ignore"):
        med = np.nanmedian(windows, axis=1)
    return np.nan_to_num(med, nan=0.0).astype(np.float32)


def _score(a: dict[str, np.ndarray]) -> np.ndarray:
    score = 0.65 * a["hist_dist"] + 0.35 * np.clip(a["pix"] * 3.5, 0.0, 1.0)
    score[0] = 0.0
    return score


def _mad(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.abs(x.astype(np.float32) - y.astype(np.float32)).mean())


def _block_means(track: Track) -> np.ndarray:
    """(n, rows*cols) mean brightness of each coarse block, one row per frame."""
    rows, cols = BLOCK_GRID
    out = np.empty((track.n, rows * cols), dtype=np.float32)
    for i, thumb in enumerate(track.thumb):
        h, w = thumb.shape
        block = thumb[: h // rows * rows, : w // cols * cols].astype(np.float32)
        out[i] = block.reshape(rows, h // rows, cols, w // cols).mean(axis=(1, 3)).reshape(-1)
    return out


def _texture(track: Track) -> np.ndarray:
    """Mean |Laplacian| per frame: photographic pictures have detail everywhere, gradients and cards do not."""
    return np.asarray([float(np.abs(cv2.Laplacian(t.astype(np.float32), cv2.CV_32F)).mean())
                       for t in track.thumb], dtype=np.float32)


def _motion_explains(before: np.ndarray, after: np.ndarray) -> bool:
    """True when `after` is mostly a warped copy of `before`: the change is camera / subject movement."""
    a8 = np.clip(before, 0, 255).astype(np.uint8)
    b8 = np.clip(after, 0, 255).astype(np.uint8)
    flow = cv2.calcOpticalFlowFarneback(a8, b8, None, 0.5, 3, 9, 3, 5, 1.1, 0)
    h, w = a8.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    moved = cv2.remap(b8, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_REFLECT)
    m = 4  # the border is not reliable after a warp
    raw = float(np.abs(b8.astype(np.float32) - a8.astype(np.float32))[m:-m, m:-m].mean())
    warped = float(np.abs(moved.astype(np.float32) - a8.astype(np.float32))[m:-m, m:-m].mean())
    return warped < 0.45 * raw


def _returns(track: Track, i: int, back: int = 4, span: int = 4) -> float:
    """How much of the jump at i comes back; 1.0 = the picture settles on something new."""
    if i < 1 or i + 1 >= track.n:
        return 1.0
    jump = _mad(track.thumb[i], track.thumb[i - 1]) + 1e-6
    best = np.inf
    for j in range(max(0, i - back), i):
        up = _mad(track.thumb[i], track.thumb[j]) + 1e-6
        if up < 0.2 * jump:
            continue
        later = min(_mad(track.thumb[k], track.thumb[j]) for k in range(i + 1, min(track.n, i + 1 + span)))
        best = min(best, later / up)
    return 1.0 if not np.isfinite(best) else float(best)


def _flash(track: Track, i: int) -> bool:
    """A strobe / muzzle flash: brightness spikes for a frame or two and the picture comes back."""
    luma = track.luma
    pre = min(luma[max(0, i - 3):i])
    rise = luma[i] - pre
    if rise <= 8.0 or i + 2 >= track.n:
        return False
    return luma[i + 2] - pre < 0.5 * rise and _returns(track, i) < 0.6


def _flash_seconds(track: Track, start: int, end: int) -> float:
    """Length of a bright burst at the head of [start, end), or 0.0 when the shot just starts bright."""
    luma = track.luma
    if start < 1 or end - start < 3:
        return 0.0
    before = float(np.median(luma[max(0, start - 3):start]))
    tail = luma[start + 1:end]
    settle = float(np.median(tail[-max(1, min(end - start - 1, int(track.fps))):]))
    jump = luma[start] - max(before, settle)
    if jump < 40.0:
        return 0.0
    k = start
    while k + 1 < end and luma[k + 1] - settle > 0.2 * jump:
        k += 1
    if k == start or k - start > track.fps * 1.5:
        return 0.0
    return (k + 1 - start) / track.fps


def _jump_cut(track: Track, i: int, a: dict[str, np.ndarray]) -> bool:
    """Same camera, subject jumps: most of the frame changes, both sides are quiet, nothing comes back."""
    if i < 4 or i + 4 > track.n or a["touch"][i] < JUMP_TOUCH:
        return False
    touch, pix = a["touch"], a["pix"]
    side_t = max(float(np.median(touch[max(0, i - 7):i])), float(np.median(touch[i + 1:i + 8])))
    side_p = max(float(np.median(pix[max(0, i - 7):i])), float(np.median(pix[i + 1:i + 8])))
    if touch[i] < 2.4 * (side_t + 0.02) or pix[i] < 2.0 * (side_p + 0.004):
        return False
    near = np.concatenate([touch[i - 2:i], touch[i + 1:i + 3]])
    if near.size and float(near.max()) > 0.5 * touch[i]:
        return False
    return _returns(track, i) >= 0.7


def _layout_swap(track: Track, i: int, a: dict[str, np.ndarray]) -> bool:
    """A card / slide swapping in front of a frozen background: colour jumps, the pixels before it are still."""
    if i < 3 or a["hist_dist"][i] < 0.22 or a["touch"][i] < SWAP_TOUCH:
        return False
    if float(a["pix"][max(0, i - 3):i].max()) > 0.006:
        return False
    return float(a["hist_dist"][i]) > 4.0 * float(a["hist_dist"][max(0, i - 3):i].max() + 0.01)


# ---------------------------------------------------------------- transitions


def _runs(mask: np.ndarray, gap: int = 2, min_len: int = 2) -> list[tuple[int, int]]:
    """Contiguous True spans, merging spans separated by at most `gap` frames."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < mask.size:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < mask.size and mask[j]:
            j += 1
        spans.append((i, j))
        i = j
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return [s for s in merged if s[1] - s[0] >= min_len]


def _blend_strength(track: Track, i: int, k: int) -> float:
    """1 - residual of M ≈ A + a(B - A); 0.0 when the middle frame is not a blend of its neighbours."""
    here = track.thumb[i].astype(np.float32)
    before = track.thumb[i - k].astype(np.float32)
    after = track.thumb[i + k].astype(np.float32)
    delta = after - before
    spread = float(np.abs(delta).mean())
    if spread < 3.0:
        return 0.0
    denom = float((delta * delta).sum())
    if denom <= 0:
        return 0.0
    alpha = float(((here - before) * delta).sum() / denom)
    if not 0.25 <= alpha <= 0.75:
        return 0.0
    resid = float(np.abs(here - (before + alpha * delta)).mean()) / (spread + 1e-6)
    if resid >= 0.22:
        return 0.0
    # an even wipe or a slow morph is a blend at every frame; a cross-fade is a burst between quiet frames
    if i >= 2 * k and i + 2 * k < track.n:
        steady = 0.5 * (float(np.abs(track.thumb[i - k] - track.thumb[i - 2 * k]).mean())
                        + float(np.abs(track.thumb[i + 2 * k] - track.thumb[i + k]).mean()))
        burst = float(np.abs(track.thumb[i + k] - track.thumb[i - k]).mean())
        if burst < 1.4 * steady:
            return 0.0
    return 1.0 - resid


def _steady_morph(track: Track, i: int, k: int) -> bool:
    """True when the picture changes as fast just outside [i-k, i+k] as inside it: a morph, not a cut away."""
    n = track.n

    def rate(a: int, b: int) -> float | None:
        a, b = max(0, a), min(n - 1, b)
        if b - a < max(2, k):
            return None
        return _mad(track.thumb[b], track.thumb[a]) * (2 * k) / (b - a)

    inside = rate(i - k, i + k)
    around = [r for r in (rate(i - 3 * k, i - k), rate(i + k, i + 3 * k)) if r is not None]
    return bool(inside and around and inside < 1.3 * max(around))


def _dissolve_runs(track: Track) -> list[tuple[int, int, float, int]]:
    n = track.n
    fps = track.fps
    if n <= 2 * fps:
        return []
    steps = sorted({max(2, int(round(fps * 0.12))), max(3, int(round(fps * 0.25))), max(5, int(round(fps * 0.5)))})
    strength = np.zeros(n, dtype=np.float32)
    for k in steps:
        for i in range(k, n - k):
            if strength[i]:
                continue
            s = _blend_strength(track, i, k)
            if s:
                strength[i] = s
    out: list[tuple[int, int, float, int]] = []
    for i, j in _runs(strength > 0, gap=2, min_len=2):
        best = i + int(np.argmax(strength[i:j]))  # the frame that sits closest to a 50 / 50 blend
        out.append((i, j, float(strength[i:j].max()), best))
    return out


def _split_picture(before: np.ndarray, here: np.ndarray, after: np.ndarray, tol: int = 6):
    """(covered, share of the picture still like `before`) for "this frame is old region + new region".

    Every pixel of a wipe frame either still looks like the shot before or already looks like the shot
    after; in a slow pan or zoom most pixels look like neither, which is what makes this a wipe test.
    """
    d_before = np.abs(here.astype(np.int16) - before.astype(np.int16))
    d_after = np.abs(here.astype(np.int16) - after.astype(np.int16))
    covered = float((np.minimum(d_before, d_after) <= tol).mean())
    share_old = float((d_before <= tol).mean())
    return covered, share_old


def _split_picture_run(track: Track, i: int, j: int, head: int, tail: int) -> bool:
    """Every frame is old region + new region, and at least one frame is a genuine 30-70 split."""
    mixed = False
    for x in range(i, j):
        covered, share_old = _split_picture(track.thumb[head], track.thumb[x], track.thumb[tail])
        if covered < 0.92:
            return False
        mixed = mixed or 0.25 <= share_old <= 0.75
    return mixed


def _changes_faster(track: Track, blocks: np.ndarray, s: int, e: int, length: int) -> bool:
    """True when [s, e] changes faster than an equally long stretch just before or just after it."""
    across = float(np.abs(blocks[e] - blocks[s]).mean()) * length / (e - s)
    rates = []
    for a, b in ((max(0, s - length), s), (e, min(track.n - 1, e + length))):
        if b - a >= max(3, length // 3):
            rates.append(float(np.abs(blocks[b] - blocks[a]).mean()) * length / (b - a))
    return not rates or across >= 2.0 * max(rates)


def _wipe_runs(track: Track) -> list[tuple[int, int, float]]:
    """A gradual transition where the new shot takes over the frame region by region.

    Windows of 0.3 / 0.6 / 1.0 / 1.6 s are scanned instead of a single frame pair: the two ends have to be
    different pictures, every frame inside has to be old blocks + new blocks (nothing that looks like
    neither), the share of new blocks has to grow without flipping back, the window has to change faster
    than its surroundings (or it is one continuous move), and the change must not be explained by motion.
    """
    n, fps = track.n, track.fps
    if n < 2 * fps:
        return []
    blocks = _block_means(track)
    texture = _texture(track)
    score = _score(track.arrays())
    found: list[tuple[int, int, float]] = []
    for seconds in WIPE_WINDOWS:
        length = max(6, int(round(seconds * fps)))
        step = max(1, length // 4)
        for s in range(0, n - length - 1, step):
            e = s + length
            busy = int((score[s + 1:e + 1] >= CUT_SCORE).sum())
            if busy > max(1, int(0.25 * length)):
                continue  # the picture jumps for many frames inside the window: a cut, not a gradual transition
            if any(s - length // 2 <= item[0] <= e + length // 2 for item in found):
                continue
            d = np.abs(blocks[s] - blocks[e])
            sig = d > max(12.0, 0.5 * float(np.median(d)))
            if float(sig.mean()) < 0.75:
                continue  # the two ends have to differ over most of the frame
            if min(float(texture[s]), float(texture[e])) < WIPE_MIN_TEXTURE:
                continue  # flat cards and gradients belong to the motion-graphics rules
            if _mad(track.thumb[s], track.thumb[e]) < 8.0:
                continue
            if not _changes_faster(track, blocks, s, e, length):
                continue
            if _motion_explains(track.thumb[s], track.thumb[e]):
                continue  # a pan or zoom whose ends differ: movement, not a replacement
            samples = [s + int(round(q * length)) for q in (0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875)]
            fits, news = [], []
            for q in samples:
                da = np.abs(blocks[q] - blocks[s])[sig]
                db = np.abs(blocks[q] - blocks[e])[sig]
                fits.append(float((np.minimum(da, db) < 0.55 * d[sig]).mean()))
                news.append(float((db < da).mean()))
            if min(fits) < 0.75:
                continue  # blocks that look like neither end: motion inside one shot
            if not (news[1] < news[3] < news[5] and news[1] <= 0.45 and news[5] >= 0.55
                    and news[5] - news[1] >= 0.35):
                continue  # the new picture has to take over gradually, not be there from the first frame
            if sum(1 for x, y in zip(news, news[1:]) if y < x) > 1:
                continue  # a block flipped back: motion
            # the window may start or end inside the transition: walk in to the frames that are fully old /
            # fully new, then test those two as the references
            head, tail = s, e - 1
            for j in range(s, e):
                da = np.abs(blocks[j] - blocks[s])[sig]
                db = np.abs(blocks[j] - blocks[e])[sig]
                if float((db < da).mean()) > 0.15:
                    break
                head = j
            for j in range(e - 1, s - 1, -1):
                da = np.abs(blocks[j] - blocks[s])[sig]
                db = np.abs(blocks[j] - blocks[e])[sig]
                if float((db < da).mean()) < 0.85:
                    break
                tail = j
            if tail - head < max(3, length // 4):
                continue
            mid = head
            for j in range(head + 1, tail):
                da = np.abs(blocks[j] - blocks[head])[sig]
                db = np.abs(blocks[j] - blocks[tail])[sig]
                if float((db < da).mean()) >= 0.5:
                    mid = j
                    break
            if not _split_picture_run(track, head + 1, tail, head, tail):
                continue  # pixel level: each frame is old region + new region
            found.append((head, tail, 0.6 + 0.3 * min(fits)))
    return found


def _mg_events(track: Track) -> list[tuple[int, int, float]]:
    """Motion-graphics beats: a frozen hold followed by another frozen hold showing a different picture
    (slide / card / title sequence). The boundary sits at the change between the two holds."""
    frozen = np.asarray(track.touch, dtype=np.float32) < FROZEN_TOUCH
    holds = _runs(frozen, gap=0, min_len=max(3, int(round(HOLD_MIN_S * track.fps))))
    out: list[tuple[int, int, float]] = []
    for (a0, a1), (b0, b1) in zip(holds, holds[1:]):
        first = track.thumb[(a0 + a1) // 2]
        second = track.thumb[(b0 + b1) // 2]
        thr = max(6.0, 0.35 * min(float(first.std()), float(second.std())))
        changed = float((np.abs(first.astype(np.float32) - second.astype(np.float32)) > thr).mean())
        if changed > 0.12:
            out.append((a1 - 1, b0 + 1, min(1.0, 0.4 + changed)))
    return out


def _fade_frames(track: Track) -> list[int]:
    """Frames standing at a brightness extreme, reached by a monotone ramp from calm picture on both sides.

    Studio-range video clips black at 16 and white at 235, so the level is compared against the plateaus
    a second away, not against a fixed threshold near 0 or 255. The monotone ramp is what separates a
    fade from a dark or blown-out *shot*: a dim shot sits at the same level before and after, so there is
    no plateau to fall from.
    """
    std = np.asarray(track.std, dtype=np.float32)
    luma = np.asarray(track.luma, dtype=np.float32)
    n = int(luma.size)
    span = max(3, int(round(track.fps * 0.5)))
    if n < 4 * span + 2:
        return []
    out: list[int] = []
    for i in range(2 * span, n - 2 * span):
        if std[i] >= 6.0:
            continue
        before = float(np.median(luma[i - 2 * span:i - span]))
        after = float(np.median(luma[i + span:i + 2 * span]))
        level = float(luma[i])
        dark = level < min(before, after) - 25.0
        bright = level > max(before, after) + 25.0
        if not (dark or bright):
            continue
        left = np.diff(luma[i - 2 * span:i + 1])
        right = np.diff(luma[i:i + 2 * span + 1])
        if int((np.abs(left) >= 2.0).sum()) < 3 or int((np.abs(right) >= 2.0).sum()) < 3:
            continue  # reached in one or two frames: a flash, not a fade
        if dark:
            ramps = float((left <= 1.0).mean()) >= 0.8 and float((right >= -1.0).mean()) >= 0.8
        else:
            ramps = float((left >= -1.0).mean()) >= 0.8 and float((right <= 1.0).mean()) >= 0.8
        if ramps and (not out or i - out[-1] > span):
            out.append(i)
    return out


# ---------------------------------------------------------------- assembly


RANK = {"fade": 3, "wipe": 2, "dissolve": 1, "cut": 0, "jump": 0, "mg": -1}


def _pick(events: list[tuple[int, int, int, str, float]]) -> tuple[dict[int, tuple[str, float]], dict[int, tuple[int, int]]]:
    """One boundary per transition.

    Each event carries the span it occupies and the frame the boundary belongs at. An editor transition
    (fade / wipe / dissolve) outranks a cut found inside its span, so a cross-fade cannot also leave a cut
    behind it; ties go to the stronger evidence.
    """
    chosen: list[tuple[int, int, int, str, float]] = []
    for ev in sorted(events, key=lambda e: (RANK.get(e[3], 0), e[4]), reverse=True):
        if any(not (ev[1] < c[0] or ev[0] > c[1]) for c in chosen):
            continue
        chosen.append(ev)
    out: dict[int, tuple[str, float]] = {}
    spans: dict[int, tuple[int, int]] = {}
    for start, end, frame, kind, strength in chosen:
        if frame in out:
            continue
        out[frame] = (kind, round(float(strength), 3))
        spans[frame] = (start, end)
    return out, spans


def _clean(boundaries: dict[int, tuple[str, float]], b: int) -> bool:
    kind, strength = boundaries[b]
    return kind in ("cut", "jump") and strength >= 0.32


def _t(track: Track, frame: int) -> float:
    times = track.times
    if times is None:
        return frame / track.fps
    if frame < len(times):
        return float(times[frame])
    step = float(np.median(np.diff(times))) if len(times) > 1 else 1 / track.fps
    return float(times[-1]) + step * (frame - len(times) + 1)


def detect(track: Track, cut_score: float = CUT_SCORE, min_shot_s: float = MIN_SHOT_S) -> list[dict]:
    n = track.n
    if n == 0:
        return []
    fps = track.fps
    a = track.arrays()
    score = _score(a)
    radius = max(4, int(round(fps * 0.5)))
    base = _rolling_median(score, radius)
    touch = a["touch"]
    touch_base = _rolling_median(touch, radius)

    events: list[tuple[int, int, str, float]] = []
    for i in range(1, n):
        s = float(score[i])
        strong = s > cut_score and s > base[i] * 2.2 + 0.05 and touch[i] > 0.22
        regional = touch[i] > 0.4 and touch[i] > touch_base[i] * 3.0 + 0.15 and s > base[i] * 3.5 + 0.03
        cut = strong or regional or _layout_swap(track, i, a)
        jump = _jump_cut(track, i, a)
        if not (cut or jump):
            continue
        if _flash(track, i) or _returns(track, i) < 0.3:
            continue
        if s < 0.45 and touch[i] < 0.5 and _motion_explains(track.thumb[max(0, i - 3)], track.thumb[min(n - 1, i + 3)]):
            continue  # a jolt / parallax that the pixel statistics cannot tell from a cut
        kind = "jump" if jump and a["hist_dist"][i] < 0.4 and touch[i] < 0.5 else "cut"
        events.append((i, i, i, kind, float(max(s, touch[i]))))

    # a transition finder that trips over one clip must not cost the hard cuts as well
    pad = int(round(fps * 0.5))
    for finder, kind, floor in ((_fade_frames, "fade", 0.9), (_dissolve_runs, "dissolve", 0.55),
                                (_wipe_runs, "wipe", 0.55), (_mg_events, "mg", 0.4)):
        try:
            found = finder(track)
        except Exception as exc:  # noqa: BLE001 - a broken transition finder must not lose the hard cuts
            print(f"shots_cv: {kind} finder skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        for item in found:
            if kind == "fade":
                frame = int(item)
                events.append((max(1, frame - pad), min(n, frame + pad), frame, kind, floor))
                continue
            start, end = int(item[0]), int(item[1])
            frame = int(item[3]) if kind == "dissolve" else end if kind == "mg" else (start + end) // 2
            events.append((start, end, frame, kind, max(floor, min(1.0, float(item[2])))))

    boundaries, spans = _pick(events)

    min_len = max(1, int(round(min_shot_s * fps)))
    short_len = max(1, int(round(SHORT_INSERT_S * fps)))
    kept: list[int] = []
    for b in sorted(boundaries):
        prev = kept[-1] if kept else 0
        if b - prev >= min_len:
            kept.append(b)
        elif (b - prev >= short_len and _clean(boundaries, b) and (not kept or _clean(boundaries, prev))
              and float(touch[prev + 1:b].max(initial=0.0)) < 0.3):
            kept.append(b)
        elif kept and boundaries[b][1] > boundaries[prev][1] and b - (kept[-2] if len(kept) > 1 else 0) >= short_len:
            kept[-1] = b
    if kept and n - kept[-1] < min_len:
        kept.pop()

    rows: list[dict] = []
    start, trans, strength = 0, "start", 0.0
    span_start = span_end = 0
    for b in kept + [n]:
        if trans == "cut" and start > 0:
            flash = _flash_seconds(track, start, b)
            if flash:
                trans = "flash"  # a bright flash right before the cut: a strobe transition, not a hard cut
        t0, t1 = _t(track, start), _t(track, b)
        rows.append({
            "index": len(rows) + 1,
            "start": round(t0, 3),
            "end": round(t1, 3),
            "duration": round(t1 - t0, 3),
            "start_frame": start,
            "end_frame": b,
            "transition_in": trans,
            "transition_start": round(_t(track, span_start), 3),
            "transition_end": round(_t(track, span_end), 3),
            "boundary_score": round(strength, 3),
            "hist_first": track.hist[start],
            "hist_last": track.hist[max(start, b - 1)],
        })
        if b < n:
            trans, strength = boundaries[b]
            span_start, span_end = spans.get(b, (b, b))
        start = b
    return rows


def detect_path(path, meta: dict) -> list[dict]:
    return detect(build_track(path, meta))
