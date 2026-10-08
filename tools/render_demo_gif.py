#!/usr/bin/env python3
"""Render PorkyHUD's fixed, synthetic, illustrative README animation."""

from __future__ import annotations

import argparse
import html
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional, Sequence


# Direct script execution starts on tools/, so select the reviewed pure artwork
# module from this checkout rather than an unrelated installed package.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_REPOSITORY_ROOT))
try:
    from porkyhud_visual import MASCOT_CYCLE, render_mascot
finally:
    sys.path.pop(0)


WIDTH = 960
HEIGHT = 600
DEFAULT_FRAMES = 36
DEFAULT_FPS = 12
MAX_FRAMES = 120
MAX_FPS = 30
MARKER = "ILLUSTRATIVE DEMO · SYNTHETIC DATA"


class RenderError(RuntimeError):
    """A closed, user-readable renderer failure."""


def validate_render_limits(frames: int, fps: int) -> None:
    if type(frames) is not int or not 1 <= frames <= MAX_FRAMES:
        raise ValueError("frames must be between 1 and %d" % MAX_FRAMES)
    if type(fps) is not int or not 1 <= fps <= MAX_FPS:
        raise ValueError("fps must be between 1 and %d" % MAX_FPS)


def safe_output_path(root: Path, output: str) -> Path:
    """Resolve one repository-contained GIF path without following symlinks."""

    if not isinstance(output, str) or not output or "\\" in output:
        raise ValueError("output must be a repository-relative GIF path")
    relative = PurePosixPath(output)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("output must stay inside the repository")
    if relative.suffix.lower() != ".gif":
        raise ValueError("output must use the .gif extension")
    try:
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("repository root does not exist") from exc
    candidate = root_resolved.joinpath(*relative.parts)
    lexical_candidate = root.joinpath(*relative.parts)
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("output path must not traverse a symlink")
    try:
        candidate.parent.resolve(strict=True).relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError("output parent must exist inside the repository") from exc
    return lexical_candidate


def _spark(values: Sequence[float]) -> str:
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[max(0, min(7, round(value / 100.0 * 7)))] for value in values)


def _mascot_html(phase: int) -> str:
    """Project shared terminal artwork as escaped, semantic colored spans."""

    frame = render_mascot(phase)
    return "\n".join(
        "".join('<span class="ink-%s">%s</span>' % (
            html.escape(span.role, quote=True), html.escape(span.text),
        ) for span in row)
        for row in frame.rows
    )


def render_html(frame: int, frames: int) -> str:
    """Return one deterministic HTML frame without reading any host state."""

    validate_render_limits(frames, DEFAULT_FPS)
    if type(frame) is not int or not 0 <= frame < frames:
        raise ValueError("frame must be inside the render sequence")
    phase = 2.0 * math.pi * frame / frames
    cpu = 38.0 + 4.0 * math.sin(phase)
    history = [38.0 + 6.0 * math.sin(phase + index * 0.46) for index in range(36)]
    history_text = _spark(history)
    # Sample a complete, bounded yaw/blink cycle, independent of host clocks.
    mascot = _mascot_html(frame * MASCOT_CYCLE // frames)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {{ --bg:#05070d; --text:#d0d0d0; --muted:#8a8a8a; --line:#474853;
    --blue:#87afd7; --green:#afd7d7; --brand:#d7afff; }}
  * {{ box-sizing:border-box; }}
  html, body {{ margin:0; width:{WIDTH}px; height:{HEIGHT}px; overflow:hidden;
    background:var(--bg); }}
  body {{ color:var(--text); font-family:"SFMono-Regular",Menlo,Consolas,monospace;
    font-size:14px; line-height:1.55; }}
  .shell {{ width:100%; height:100%; padding:24px 26px 20px; display:grid;
    grid-template-rows:68px 32px 146px 1fr 38px 21px; gap:10px; }}
  .top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .headline {{ font-weight:700; color:var(--green); font-size:16px; white-space:nowrap; }}
  .reason {{ margin-top:7px; color:var(--text); font-size:12px; }}
  .demo {{ color:var(--muted); font-size:10px; white-space:nowrap;
    border:1px solid var(--line); padding:4px 7px; }}
  .ribbon {{ color:var(--blue); display:flex; gap:26px; font-size:12px;
    border-bottom:1px solid var(--line); align-items:flex-start; font-weight:600; }}
  .ribbon span {{ white-space:nowrap; }}
  .signature {{ display:flex; align-items:flex-start; border-bottom:1px solid var(--line);
    gap:32px; overflow:hidden; }}
  .mascot {{ font-family:inherit; font-size:16px; line-height:19px; margin:0;
    letter-spacing:0; font-weight:400; white-space:pre; flex-shrink:0; }}
  .ink-brand {{ color:#ff87af; }} .ink-highlight {{ color:#ffd7d7; }}
  .ink-shadow {{ color:#d787af; }} .ink-detail {{ color:#eeeeee; }}
  .ink-structure {{ color:var(--muted); }}
  .identity {{ padding-top:8px; }}
  .wordmark {{ font-size:20px; font-weight:600; margin-bottom:17px; letter-spacing:.035em; }}
  .tagline {{ color:var(--blue); font-size:12px; letter-spacing:.07em; }}
  .machine {{ margin-top:5px; font-size:12px; }}
  .profile {{ color:var(--muted); font-size:11px; margin-top:4px; }}
  .content {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; min-height:0; }}
  .panel {{ border:1px solid var(--line); border-radius:8px; padding:15px 15px 12px;
    position:relative; min-height:0; }}
  .title {{ position:absolute; top:-10px; left:14px; padding:0 7px;
    background:var(--bg); color:var(--muted); font-size:12px; font-weight:700; }}
  .facts {{ display:grid; grid-template-columns:65px 1fr; row-gap:5px;
    color:var(--blue); font-size:13px; margin-top:3px; }}
  .facts .label {{ font-weight:600; }}
  .provenance {{ color:var(--muted); font-size:10px; margin-top:14px; }}
  .spark {{ color:var(--blue); font-size:13px; white-space:nowrap; margin-top:8px; }}
  .spark-label {{ color:var(--muted); font-size:10px; margin-right:5px; }}
  .workloads {{ display:grid; grid-template-columns:1fr auto; row-gap:7px;
    margin-top:2px; font-size:13px; }}
  .workloads .value {{ color:var(--text); font-variant-numeric:tabular-nums; }}
  .workload-note {{ margin-top:12px; color:var(--muted); font-size:10px; }}
  .context {{ display:flex; align-items:center; gap:18px; color:var(--blue);
    font-size:11px; border-top:1px solid var(--line); white-space:nowrap; }}
  .context .label {{ color:var(--muted); font-size:10px; }}
  .footer {{ display:flex; justify-content:space-between; align-items:flex-end;
    color:var(--muted); font-size:11px; white-space:nowrap; }}
  .footer .key {{ color:var(--text); }}
</style>
</head>
<body>
<main class="shell">
  <header class="top">
    <div>
      <div class="headline">PorkyHUD · 1 OVERVIEW · NORMAL · LIVE</div>
      <div class="reason">No active CPU, memory, thermal, or storage pressure.</div>
    </div>
    <div class="demo">{MARKER}</div>
  </header>
  <section class="ribbon" aria-label="Decision evidence">
    <span>CPU {cpu:.1f}% · 0s</span><span>MEM NORMAL · 0s</span>
    <span>THERM CLEAR · 0s</span><span>DISK 312 GiB FREE · 0s</span>
  </section>
  <section class="signature">
    <pre class="mascot" aria-label="Shared sculpted PorkyHUD mascot">{mascot}</pre>
    <div class="identity">
      <div class="wordmark">P O R K Y H U D</div>
      <div class="tagline">YOUR MAC, IN FOCUS</div>
      <div class="machine">Synthetic Apple Silicon · 64 GiB</div>
      <div class="profile">Graphite · sculpted · motion calm (optional)</div>
    </div>
  </section>
  <section class="content">
    <article class="panel">
      <div class="title">EVIDENCE DETAIL</div>
      <div class="facts">
        <div class="label">CPU</div><div>{cpu:.1f}% · LIVE · 15s window</div>
        <div class="label">MEM</div><div>NORMAL · LIVE</div>
        <div class="label">THERM</div><div>CLEAR · LIVE</div>
        <div class="label">DISK</div><div>312 GiB free · LIVE</div>
      </div>
      <div class="provenance">PROVENANCE · macOS core evidence</div>
      <div class="spark"><span class="spark-label">CPU 60s</span>{history_text}</div>
    </article>
    <article class="panel">
      <div class="title">TOP PROCESSES · CPU</div>
      <div class="workloads">
        <div>Creative workload</div><div class="value">14.2%</div>
        <div>Browser workspace</div><div class="value">9.4%</div>
        <div>Build workers</div><div class="value">6.8%</div>
        <div>Background services</div><div class="value">4.1%</div>
        <div>Terminal workspace</div><div class="value">1.3%</div>
      </div>
      <div class="workload-note">Identity only · m sort · advanced sensors CHECKING</div>
    </article>
  </section>
  <div class="context"><span class="label">EXPLORE ·</span>
    <span>7 Insights · 8 Devices · 9 Network · 0 TCP connections</span>
  </div>
  <footer class="footer">
    <span><span class="key">d</span> Explore · 1–5 / 7 modes · a motion · t theme · h help · q quit</span>
    <span>Dashboard motion starts off</span>
  </footer>
</main>
</body>
</html>"""


def _find_executable(names: Sequence[str]) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RenderError("subprocess cleanup failed") from exc
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RenderError("subprocess cleanup failed") from exc
    if process.poll() is None:
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise RenderError("subprocess cleanup exceeded its deadline") from exc
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise RenderError("subprocess cleanup failed") from exc
        if time.monotonic() >= deadline:
            raise RenderError("subprocess cleanup exceeded its deadline")
        time.sleep(0.01)


def _run(command: Sequence[str], timeout_s: float, label: str) -> None:
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        process = subprocess.Popen(
            list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
        )
        process.communicate(timeout=timeout_s)
        returncode = process.returncode
        _terminate_process_group(process)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _terminate_process_group(process)
        raise RenderError("%s timed out" % label) from exc
    except OSError as exc:
        if process is not None:
            _terminate_process_group(process)
        raise RenderError("%s failed" % label) from exc
    except BaseException:
        if process is not None:
            _terminate_process_group(process)
        raise
    if returncode != 0:
        raise RenderError("%s exited with status %d" % (label, returncode))


def render(root: Path, output: str, frames: int, fps: int) -> Path:
    validate_render_limits(frames, fps)
    destination = safe_output_path(root, output)
    chrome = _find_executable((
        "google-chrome", "chromium", "chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ))
    ffmpeg = _find_executable(("ffmpeg",))
    if chrome is None:
        raise RenderError("Chrome or Chromium is required to render the demo")
    if ffmpeg is None:
        raise RenderError("ffmpeg is required to encode the demo")
    with tempfile.TemporaryDirectory(prefix="porkyhud-synthetic-demo-") as temporary:
        temp = Path(temporary)
        for index in range(frames):
            html_path = temp / ("frame-%03d.html" % index)
            png_path = temp / ("frame-%03d.png" % index)
            html_path.write_text(render_html(index, frames), encoding="utf-8")
            _run(
                [
                    chrome, "--headless=new", "--disable-gpu",
                    "--disable-background-networking", "--hide-scrollbars",
                    "--force-device-scale-factor=1",
                    "--window-size=%d,%d" % (WIDTH, HEIGHT),
                    "--screenshot=%s" % png_path,
                    html_path.resolve().as_uri(),
                ],
                20.0,
                "Chrome frame render",
            )
            if not png_path.is_file():
                raise RenderError("Chrome did not create a frame")
        encoded = temp / "porkyhud-demo.gif"
        _run(
            [
                ffmpeg, "-v", "error", "-y", "-framerate", str(fps),
                "-i", os.fspath(temp / "frame-%03d.png"),
                "-vf",
                "fps=%d,scale=%d:-1:flags=lanczos,split[s0][s1];"
                "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
                "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
                % (fps, WIDTH),
                "-loop", "0", os.fspath(encoded),
            ],
            60.0,
            "ffmpeg GIF encode",
        )
        if not encoded.is_file() or encoded.stat().st_size == 0:
            raise RenderError("ffmpeg did not create a GIF")
        os.replace(encoded, destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the fixed synthetic PorkyHUD README demo.",
    )
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--output", default="assets/porkyhud-demo.gif")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        path = render(root, args.output, args.frames, args.fps)
    except (RenderError, ValueError) as exc:
        parser.exit(1, "error: %s\n" % exc)
    print("Rendered fixed synthetic demo: %s" % path.relative_to(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
