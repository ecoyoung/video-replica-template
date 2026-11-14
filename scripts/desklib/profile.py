"""Per-shot look: how bright, how contrasty, how saturated, how warm, and whether the camera moves.

One low-resolution decode pass (64 px wide RGB) is enough. Camera motion needs OpenCV's optical flow and
is optional: without numpy + opencv the standard-library path still reports tone and motion strength.

The point is not to describe the picture for a human — it is to give the agent writing the prompts a few
measured facts ("this shot is dim and cool, the camera is locked off") instead of asking it to guess from
a single still, and to let the handoff warn when the six generated units would not match in colour.
"""

from __future__ import annotations

import math

from desklib import media

PROFILE_W = 64          # decode width for the tone statistics
MOTION_W = 48           # thumbs kept for the optical-flow check
SAMPLES = 600           # pixels sampled per frame on the standard-library path
MOTION_SAMPLE = 96      # pixel positions used for the cheap motion signature

LUMA = (0.299, 0.587, 0.114)

def _summary(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _frame_numbers(rgb: bytes, numpy_ok: bool, previous):
    """(luma_mean, contrast, saturation, warmth, motion_signature) for one RGB frame."""
    if numpy_ok:
        import numpy as np

        arr = np.frombuffer(rgb, dtype=np.uint8).reshape(-1, 3).astype(np.float32)
        luma = arr[:, 0] * LUMA[0] + arr[:, 1] * LUMA[1] + arr[:, 2] * LUMA[2]
        mx = arr.max(axis=1)
        sat = float(np.where(mx > 0, (mx - arr.min(axis=1)) / np.maximum(mx, 1.0), 0.0).mean()) * 100.0
        warmth = float(arr[:, 0].mean() - arr[:, 2].mean()) / 2.55
        signature = luma.mean() + 0.0 * luma[:0]  # keep the type stable
        step = max(1, luma.size // MOTION_SAMPLE)
        sig = luma[::step][:MOTION_SAMPLE]
        return (float(luma.mean()), float(luma.std()), sat, warmth, sig)

    count = len(rgb) // 3
    step = max(1, count // SAMPLES)
    lumas, sats, reds, blues = [], [], 0, 0
    sig = []
    sig_step = max(1, len(rgb) // MOTION_SAMPLE)
    for index in range(0, len(rgb) - 2, 3 * step):
        r, g, b = rgb[index], rgb[index + 1], rgb[index + 2]
        lumas.append(r * LUMA[0] + g * LUMA[1] + b * LUMA[2])
        mx, mn = max(r, g, b), min(r, g, b)
        sats.append((mx - mn) / mx if mx else 0.0)
        reds += r
        blues += b
    for index in range(0, len(rgb) - 2, sig_step):
        sig.append(rgb[index])
    mean = sum(lumas) / len(lumas) if lumas else 0.0
    var = sum((x - mean) ** 2 for x in lumas) / len(lumas) if lumas else 0.0
    n = max(1, len(lumas))
    return (mean, math.sqrt(var), sum(sats) / n * 100.0, (reds - blues) / n / 2.55, sig)


def _flow_stats(gray_a, gray_b) -> tuple[float, float, float, float]:
    """(mean magnitude, translation, residual around the translation, radial component) of optical flow."""
    import cv2
    import numpy as np

    a8 = np.clip(gray_a, 0, 255).astype(np.uint8)
    b8 = np.clip(gray_b, 0, 255).astype(np.uint8)
    flow = cv2.calcOpticalFlowFarneback(a8, b8, None, 0.5, 3, 9, 3, 5, 1.1, 0)
    fx, fy = flow[..., 0], flow[..., 1]
    mag = float(np.hypot(fx, fy).mean())
    dx, dy = float(fx.mean()), float(fy.mean())
    translation = math.hypot(dx, dy)
    residual = float(np.hypot(fx - dx, fy - dy).mean())
    h, w = a8.shape
    ys, xs = np.mgrid[0:h, 0:w]
    rx, ry = xs - w / 2.0, ys - h / 2.0
    norm = np.maximum(np.hypot(rx, ry), 1.0)
    radial = float(((fx * rx + fy * ry) / norm).mean())
    return mag, translation, residual, radial


def _classify_flow(samples: list[tuple[float, float, float, float]]) -> tuple[str, float]:
    """Median of a few frame pairs -> (kind, strength). Defaults to static unless the evidence is clear:
    calling a locked-off product shot "handheld" is the one mistake that hurts the prompt."""
    if not samples:
        return "static", 0.0
    mag = sorted(s[0] for s in samples)[len(samples) // 2]
    translation = sorted(s[1] for s in samples)[len(samples) // 2]
    residual = sorted(s[2] for s in samples)[len(samples) // 2]
    radial = sorted(s[3] for s in samples)[len(samples) // 2]
    strength = min(100.0, mag * 20.0)
    if mag < 0.9:
        return "static", strength
    if translation > 0.6 and residual < 0.75 * mag and abs(radial) < 0.5 * mag:
        return "pan", strength
    if abs(radial) > 0.45 * mag and translation < 0.65 * mag:
        return "zoom", strength
    return "handheld", strength


def shot_profiles(path, meta: dict, rows: list[dict]) -> list[dict]:
    """One look dict per shot row (brightness / contrast / saturation / warmth / motion)."""
    numpy_ok = _have_numpy()
    try:
        import cv2  # noqa: F401

        flow_ok = True
    except ImportError:
        flow_ok = False

    n = len(rows)
    width, height = media.frame_size(meta, PROFILE_W)  # a source narrower than PROFILE_W is not upscaled
    track_fps = float(meta.get("fps") or 30.0)
    frames: list[tuple[float, float, float, float]] = []   # tone per frame
    signatures: list = []                                  # for motion
    thumbs: list = []                                      # small gray thumbs (flow path)
    for rgb in media.iter_raw(path, meta, PROFILE_W, "rgb24"):
        luma, contrast, sat, warmth, sig = _frame_numbers(rgb, numpy_ok, None)
        frames.append((luma, contrast, sat, warmth))
        signatures.append(sig)
        if flow_ok:
            import numpy as np

            arr = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3).astype(np.float32)
            gray = arr[..., 0] * LUMA[0] + arr[..., 1] * LUMA[1] + arr[..., 2] * LUMA[2]
            thumbs.append(gray[:, :: max(1, width // MOTION_W)].astype("uint8"))
    if not frames:
        return [{} for _ in range(n)]

    def rate(sig_a, sig_b) -> float:
        if numpy_ok:
            import numpy as np

            return float(np.abs(sig_a - sig_b).mean()) / 255.0
        return sum(abs(a - b) for a, b in zip(sig_a, sig_b)) / max(1, len(sig_a)) / 255.0

    out: list[dict] = []
    for row in rows:
        a = max(0, int(row.get("start_frame") or 0))
        b = min(len(frames), max(a + 1, int(row.get("end_frame") or a + 1)))
        window = frames[a:b]
        if not window:
            out.append({})
            continue
        brightness = sum(w[0] for w in window) / len(window) / 2.55
        contrast = sum(w[1] for w in window) / len(window) / 2.55
        saturation = sum(w[2] for w in window) / len(window)
        warmth = sum(w[3] for w in window) / len(window)
        strength = 0.0
        if len(signatures) > a + 1:
            strength = sum(rate(signatures[i - 1], signatures[i]) for i in range(a + 1, min(b, len(signatures))))
            strength = strength / max(1, min(b, len(signatures)) - a - 1) * 100.0
        kind = "static" if strength < 1.2 else ("slow" if strength < 4.0 else "moving")
        if flow_ok and b - a >= 4:
            gap = max(2, int(round(0.2 * float(track_fps))))
            samples = []
            for q in (0.2, 0.5, 0.8):
                first = a + int(q * (b - a))
                second = min(len(thumbs) - 1, first + gap)
                if second - first >= 2 and second < len(thumbs):
                    try:
                        samples.append(_flow_stats(thumbs[first], thumbs[second]))
                    except Exception:  # noqa: BLE001 - flow is a bonus, never a failure
                        pass
            if samples:
                kind, flow_strength = _classify_flow(samples)
                strength = max(strength, flow_strength)
        out.append({
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "saturation": round(saturation, 1),
            "warmth": round(warmth, 1),
            "motion": kind,
            "motionStrength": round(min(100.0, strength), 1),
        })
    return out


def unit_look(profiles: list[dict], rows: list[dict], unit: dict) -> dict:
    """Duration-weighted summary of the shot profiles covering one unit."""
    inside = [(p, float(r["duration"])) for p, r in zip(profiles, rows)
              if r["start"] >= unit["start"] - 0.05 and r["end"] <= unit["end"] + 0.05 and p]
    if not inside:
        return {}
    weights = [w for _p, w in inside]
    look = {
        "brightness": round(_summary([p["brightness"] for p, _ in inside], weights), 1),
        "contrast": round(_summary([p["contrast"] for p, _ in inside], weights), 1),
        "saturation": round(_summary([p["saturation"] for p, _ in inside], weights), 1),
        "warmth": round(_summary([p["warmth"] for p, _ in inside], weights), 1),
        "motion": inside[0][0].get("motion") or "static",
        "motionStrength": round(_summary([p.get("motionStrength") or 0.0 for p, _ in inside], weights), 1),
    }
    return look


MOTION_TEXT = {
    "static": "固定机位",
    "pan": "运镜（横移/摇）",
    "zoom": "推拉",
    "handheld": "手持晃动",
    "slow": "轻微移动",
    "moving": "画面有明显运动",
}


def look_text(look: dict) -> str:
    """A short Chinese phrase for prompts and the handoff table."""
    if not look:
        return ""
    parts = []
    parts.append(MOTION_TEXT.get(str(look.get("motion")), "固定机位"))
    bright = float(look.get("brightness") or 0.0)
    parts.append("很暗" if bright < 22 else "偏暗" if bright < 38 else "明亮" if bright > 68 else "中等亮度")
    warmth = float(look.get("warmth") or 0.0)
    if warmth > 6:
        parts.append("偏暖")
    elif warmth < -6:
        parts.append("偏冷")
    sat = float(look.get("saturation") or 0.0)
    if sat > 55:
        parts.append("高饱和")
    elif sat < 18:
        parts.append("低饱和")
    return " · ".join(parts)


def warnings(units: list[dict]) -> list[str]:
    """Colour / brightness jumps between neighbouring units: the six generated clips have to match."""
    out: list[str] = []
    for prev, cur in zip(units, units[1:]):
        a, b = prev.get("look") or {}, cur.get("look") or {}
        if not a or not b:
            continue
        dw = float(b.get("warmth") or 0.0) - float(a.get("warmth") or 0.0)
        db = float(b.get("brightness") or 0.0) - float(a.get("brightness") or 0.0)
        if abs(dw) >= 12:
            out.append(f"段{prev['index']}→段{cur['index']} 色温跳变 {dw:+.0f}（{'暖转冷' if dw < 0 else '冷转暖'}），"
                       "生成后建议统一调色")
        if abs(db) >= 20:
            out.append(f"段{prev['index']}→段{cur['index']} 亮度跳变 {db:+.0f}（{'变暗' if db < 0 else '变亮'}），"
                       "注意曝光一致")
    return out
