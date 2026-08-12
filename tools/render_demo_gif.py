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


def _bar(percent: float, cells: int = 20) -> str:
    filled = max(0, min(cells, round(percent / 100.0 * cells)))
    return "█" * filled + "░" * (cells - filled)


def _spark(values: Sequence[float]) -> str:
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[max(0, min(7, round(value / 100.0 * 7)))] for value in values)


def render_html(frame: int, frames: int) -> str:
    """Return one deterministic HTML frame without reading any host state."""

    validate_render_limits(frames, DEFAULT_FPS)
    if type(frame) is not int or not 0 <= frame < frames:
        raise ValueError("frame must be inside the render sequence")
    phase = 2.0 * math.pi * frame / frames
    cpu = 38.0 + 4.0 * math.sin(phase)
    memory = 57.0 + 1.5 * math.sin(phase * 0.5)
    history = [35.0 + 7.0 * math.sin(phase + index * 0.46) for index in range(28)]
    history_text = _spark(history)
    cpu_bar = _bar(cpu)
    memory_bar = _bar(memory)
    pulse = 0.50 + 0.14 * math.sin(phase)
    elapsed = frame / DEFAULT_FPS
    synthetic_time = "12:34:%02d" % int(elapsed)
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { --bg:#070a0f; --panel:#0c1119; --line:#263341; --text:#e8f2f4;
    --muted:#82919c; --cyan:#69e7ef; --green:#82e4aa; --amber:#f2c56b;
    --red:#ff746f; --pulse:%.3f; }
  * { box-sizing:border-box; }
  html, body { margin:0; width:%dpx; height:%dpx; overflow:hidden; background:var(--bg); }
  body { color:var(--text); font-family:"SFMono-Regular",Menlo,Consolas,monospace;
    font-size:14px; letter-spacing:.01em; }
  .shell { width:100%%; height:100%%; padding:20px 22px 18px; display:grid;
    grid-template-rows:auto auto 1fr auto; gap:11px;
    background:radial-gradient(circle at 82%% 0,rgba(105,231,239,.07),transparent 34%%),var(--bg); }
  .top { display:flex; align-items:flex-start; justify-content:space-between; }
  .brand { color:var(--cyan); font-size:20px; font-weight:800; letter-spacing:.13em; }
  .mode { color:var(--muted); margin-top:4px; font-size:12px; letter-spacing:.06em; }
  .demo { color:#c8f4f6; border:1px solid #3a6870; border-radius:999px; padding:7px 11px;
    font-size:11px; font-weight:700; letter-spacing:.055em; background:#0b1920; }
  .ribbon { display:grid; grid-template-columns:1.18fr repeat(4,1fr); border:1px solid var(--line);
    border-radius:9px; background:#0a0f16; overflow:hidden; }
  .source { padding:10px 13px; border-right:1px solid var(--line); }
  .source:last-child { border-right:0; }
  .source .label { color:var(--muted); font-size:10px; letter-spacing:.11em; }
  .source .value { margin-top:5px; color:var(--green); font-weight:700; }
  .source.checking .value { color:var(--amber); }
  .content { display:grid; grid-template-columns:1.25fr .92fr; gap:11px; min-height:0; }
  .column { display:grid; gap:11px; min-height:0; }
  .column.left { grid-template-rows:1.14fr .86fr; }
  .column.right { grid-template-rows:1fr 1fr; }
  .panel { border:1px solid var(--line); border-radius:9px; background:var(--panel);
    padding:13px 15px; overflow:hidden; }
  .panel-title { color:var(--cyan); font-size:11px; font-weight:800; letter-spacing:.12em;
    display:flex; justify-content:space-between; margin-bottom:10px; }
  .panel-title span { color:var(--muted); font-weight:500; letter-spacing:.02em; }
  .decision { display:flex; align-items:center; gap:13px; margin:4px 0 10px; }
  .decision .state { color:var(--green); font-size:30px; font-weight:800; letter-spacing:.04em;
    text-shadow:0 0 18px rgba(130,228,170,var(--pulse)); }
  .decision .copy { color:#b8c5ca; line-height:1.45; }
  .metric-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px 17px; }
  .metric { white-space:pre; line-height:1.7; }
  .metric .name { color:var(--muted); }
  .metric .bar { color:var(--cyan); }
  .metric .number { color:#fff; }
  .spark { color:var(--cyan); font-size:17px; letter-spacing:.045em; white-space:nowrap; }
  .muted { color:var(--muted); }
  .facts { display:grid; grid-template-columns:auto 1fr auto; row-gap:8px; column-gap:11px; }
  .facts .dot { color:var(--green); } .facts .value { color:#d6e2e5; text-align:right; }
  .workloads { display:grid; grid-template-columns:1fr 48px 48px; row-gap:8px; column-gap:8px; }
  .workloads .head { color:var(--muted); font-size:10px; letter-spacing:.08em; }
  .workloads > div:nth-child(3n+2), .workloads > div:nth-child(3n+3) { text-align:right; }
  .footer { color:var(--muted); display:flex; justify-content:space-between; font-size:11px; }
  .footer .keys { color:#adc0c6; }
</style>
</head>
<body>
<main class="shell">
  <header class="top">
    <div><div class="brand">PORKYHUD</div><div class="mode">1 OVERVIEW · %s · MOTION OFF DEFAULT</div></div>
    <div class="demo">%s</div>
  </header>
  <section class="ribbon">
    <div class="source"><div class="label">DECISION INPUTS</div><div class="value">4 / 4 LIVE</div></div>
    <div class="source"><div class="label">CPU</div><div class="value">LIVE · 0s</div></div>
    <div class="source"><div class="label">MEMORY PRESSURE</div><div class="value">LIVE · NORMAL</div></div>
    <div class="source"><div class="label">OS THERMAL</div><div class="value">LIVE · CLEAR</div></div>
    <div class="source"><div class="label">STORAGE</div><div class="value">LIVE · 43 GiB</div></div>
  </section>
  <section class="content">
    <div class="column left">
      <article class="panel">
        <div class="panel-title">SYSTEM READ <span>evidence age 0s</span></div>
        <div class="decision"><div class="state">NORMAL</div><div class="copy">No typed decision input currently indicates<br>resource or thermal pressure.</div></div>
        <div class="metric-grid">
          <div class="metric"><span class="name">CPU </span><span class="bar">%s</span> <span class="number">%2.0f%%</span></div>
          <div class="metric"><span class="name">RAM </span><span class="bar">%s</span> <span class="number">%2.0f%%</span></div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-title">60 SECOND CONTEXT <span>descriptive · not severity</span></div>
        <div class="spark">%s</div>
        <div class="muted" style="margin-top:12px">steady sampling · bounded collectors · last good values retained</div>
      </article>
    </div>
    <div class="column right">
      <article class="panel">
        <div class="panel-title">EVIDENCE <span>typed inputs only</span></div>
        <div class="facts">
          <div class="dot">●</div><div>CPU window</div><div class="value">38%% · 15s</div>
          <div class="dot">●</div><div>Memory pressure</div><div class="value">NORMAL</div>
          <div class="dot">●</div><div>OS thermal state</div><div class="value">CLEAR</div>
          <div class="dot">●</div><div>Storage headroom</div><div class="value">43 GiB</div>
          <div class="muted">○</div><div class="muted">Advanced sensors</div><div class="muted value">CHECKING</div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-title">WORKLOADS <span>synthetic categories</span></div>
        <div class="workloads">
          <div class="head">IDENTITY</div><div class="head">CPU</div><div class="head">MEM</div>
          <div>creative workload</div><div>14%%</div><div>8%%</div>
          <div>browser workload</div><div>9%%</div><div>12%%</div>
          <div>background services</div><div>4%%</div><div>5%%</div>
        </div>
      </article>
    </div>
  </section>
  <footer class="footer"><span class="keys">1–5 mode · m sort · h help · r refresh · q quit</span><span>SYNTHETIC SYSTEM · %s</span></footer>
</main>
</body>
</html>""" % (
        pulse, WIDTH, HEIGHT, synthetic_time, MARKER, cpu_bar, cpu,
        memory_bar, memory, history_text, synthetic_time,
    )


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
