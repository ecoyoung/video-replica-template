"""ffmpeg / ffprobe helpers (standard library only)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Iterator

FFMPEG_TIMEOUT = 600
AUDIO_RATE = 44100


class MediaError(RuntimeError):
    pass


def have_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} 不在 PATH 里：切镜、抽帧和拼接都需要 ffmpeg")


def ff(args: list[str], timeout: int = FFMPEG_TIMEOUT) -> None:
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 失败：{proc.stderr.decode(errors='ignore')[-300:]}")


def _ratio(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    if "/" in value:
        a, b = value.split("/", 1)
        try:
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


ASPECTS = (("9:16", 9 / 16), ("3:4", 3 / 4), ("6:7", 6 / 7), ("1:1", 1.0), ("4:3", 4 / 3), ("16:9", 16 / 9), ("21:9", 21 / 9))


def aspect_name(width: int, height: int) -> str:
    if not width or not height:
        return "unknown"
    r = width / height
    for name, val in ASPECTS:
        if abs(r - val) / val < 0.04:
            return name
    return f"{width}:{height}"


def probe(path: str | Path) -> dict:
    """{width, height (displayed, rotation applied), fps, duration, nbFrames, hasAudio, aspectRatio, rotation}"""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe 读不了：{proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    if not video:
        raise MediaError("文件里没有视频流")
    v = video[0]
    fmt = data.get("format") or {}
    fps = _ratio(v.get("avg_frame_rate")) or _ratio(v.get("r_frame_rate")) or 30.0
    duration = float(v.get("duration") or fmt.get("duration") or 0.0)
    rotation = 0
    for side in v.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(abs(float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    if "rotate" in (v.get("tags") or {}):
        try:
            rotation = int(v["tags"]["rotate"]) % 360
        except ValueError:
            pass
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    if rotation in (90, 270):
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "duration": round(duration, 3),
        "nbFrames": int(v.get("nb_frames") or 0) or int(round(duration * fps)),
        "hasAudio": any(s.get("codec_type") == "audio" for s in streams),
        "aspectRatio": aspect_name(width, height),
        "rotation": rotation,
    }


def scaled_size(meta: dict, width: int) -> tuple[int, int]:
    w, h = int(meta.get("width") or 0), int(meta.get("height") or 0)
    width = min(width, w) if w else width
    width -= width % 2
    height = int(round(h * width / w)) if w else width
    height -= height % 2
    return max(2, width), max(2, height)


@lru_cache(maxsize=1)
def _passthrough() -> tuple[str, ...]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
    major, minor = (int(m.group(1)), int(m.group(2))) if m else (7, 0)
    return ("-fps_mode", "passthrough") if (major, minor) >= (5, 1) else ("-vsync", "passthrough")


def iter_raw(path: Path, meta: dict, width: int, pix: str) -> Iterator[bytes]:
    """Every decoded frame (no fps resampling) as raw bytes at the given width; pix = gray | rgb24."""
    w, h = scaled_size(meta, width)
    bpp = 1 if pix == "gray" else 3
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-an", "-sn", "-vf", f"scale={w}:{h}:flags=area",
           *_passthrough(), "-f", "rawvideo", "-pix_fmt", pix, "-"]
    size = w * h * bpp
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
    count = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            count += 1
            yield buf
        if count == 0:
            proc.wait()
            errf.seek(0)
            err = errf.read()[-2000:].decode(errors="ignore").strip()
            raise MediaError(f"ffmpeg 没解出画面：{err[:300]}")
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
        errf.close()


def frame_size(meta: dict, width: int) -> tuple[int, int]:
    return scaled_size(meta, width)


def frame_times(path: Path, n: int, fps: float) -> list[float] | None:
    """Real pts of variable-frame-rate video, or None when frame / fps is accurate ."""
    if n < 2 or fps <= 0:
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=120).stdout
        pts = sorted(float(x.strip().rstrip(",")) for x in out.split() if x.strip().rstrip(",") not in ("", "N/A"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if len(pts) != n:
        return None
    t = [p - pts[0] for p in pts]
    if max(abs(t[i] - i / fps) for i in range(n)) <= 0.5 / fps:
        return None
    return t


def extract_frame(path: Path, t: float, out: Path, width: int | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale={width}:-2"] if width else []
    out.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t, 0):.4f}", "-i", str(path), "-frames:v", "1", *vf, "-q:v", "2", str(out)]
    if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.is_file():
        cmd = ["ffmpeg", "-v", "error", "-y", "-sseof", "-3", "-i", str(path), "-update", "1", *vf, "-q:v", "2", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
    return out
