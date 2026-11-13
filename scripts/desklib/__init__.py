"""Shot-based video replica: reference video -> recipe (template) -> per-unit generation plan -> assembly.

Standard library only. ffmpeg/ffprobe are required for ingest and assembly. Shot detection runs on the
standard library (desklib.shots_lite: cuts, jump cuts, flashes, fades); installing numpy + opencv swaps in
desklib.shots_cv, which also resolves dissolves and wipes.
No network access, no API keys: every model call (watching the video, images, videos, voice) is made by
the calling agent with its own tools.
"""
