"""Pure terminal artwork: bounded frames, semantic ink, and stable cell geometry.

The mascot is a tiny orthographic sculpture rather than a changing ASCII face.
Its ellipsoids and ear facets are sampled at eight dots per terminal cell; callers
own colors, clocks, motion preferences, and every terminal write.
"""

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Optional, Tuple


MASCOT_WIDTH = 28
MASCOT_HEIGHT = 7
MASCOT_CYCLE = 96
MASCOT_STYLES = ("sculpted", "outline", "ascii")
VISUAL_ROLES = ("brand", "highlight", "shadow", "detail", "structure")


@dataclass(frozen=True)
class VisualSpan:
    text: str
    role: str


@dataclass(frozen=True)
class MascotFrame:
    rows: Tuple[Tuple[VisualSpan, ...], ...]
    width: int = MASCOT_WIDTH
    height: int = MASCOT_HEIGHT

    @property
    def plain_rows(self) -> Tuple[str, ...]:
        return tuple("".join(span.text for span in row) for row in self.rows)


# x, y, z, x/y/z radii, material. Positive z faces the observer.
# Features are actual raised/occluding surfaces, so their parallax follows the
# same projection as the silhouette. Dark eyes and nostrils are negative ink.
_ELLIPSOIDS = (
    (0.0, 0.20, 0.0, 0.87, 0.77, 0.56, "skin"),
    (-0.49, -0.07, 0.30, 0.33, 0.30, 0.23, "skin"),
    (0.49, -0.07, 0.30, 0.33, 0.30, 0.23, "skin"),
    (0.0, 0.03, 0.61, 0.38, 0.23, 0.18, "snout"),
    (-0.15, 0.055, 0.785, 0.051, 0.073, 0.038, "hole"),
    (0.15, 0.055, 0.785, 0.051, 0.073, 0.038, "hole"),
    (0.0, -0.32, 0.487, 0.25, 0.065, 0.033, "hole"),
    (0.0, -0.278, 0.52, 0.27, 0.063, 0.035, "skin"),
)
_EARS = (
    ((-0.82, 0.62, 0.06), (-1.00, 1.25, -0.10), (-0.33, 0.83, 0.12), "skin"),
    ((0.82, 0.62, 0.06), (1.00, 1.25, -0.10), (0.33, 0.83, 0.12), "skin"),
    ((-0.76, 0.79, 0.08), (-0.88, 1.10, -0.045), (-0.48, 0.86, 0.14), "inner"),
    ((0.76, 0.79, 0.08), (0.88, 1.10, -0.045), (0.48, 0.86, 0.14), "inner"),
)
_BRAILLE_BITS = ((1, 8), (2, 16), (4, 32), (64, 128))


def _ellipsoid_hit(
    sx: float, sy: float, cosine: float, sine: float,
    shape: Tuple[float, float, float, float, float, float, str],
) -> Optional[Tuple[float, str, float]]:
    cx, cy, cz, rx, ry, rz, material = shape
    # Inverse-rotated orthographic ray: world (x,z) at view depth t.
    ox, oy, oz = sx * cosine - cx, sy - cy, sx * sine - cz
    dx, dz = -sine, cosine
    a = (dx / rx) ** 2 + (dz / rz) ** 2
    b = 2.0 * (ox * dx / rx ** 2 + oz * dz / rz ** 2)
    c = (ox / rx) ** 2 + (oy / ry) ** 2 + (oz / rz) ** 2 - 1.0
    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return None
    depth = (-b + math.sqrt(discriminant)) / (2.0 * a)
    nx = (ox + depth * dx) / rx ** 2
    ny = oy / ry ** 2
    nz = (oz + depth * dz) / rz ** 2
    norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    # A fixed soft key light above the left cheek: light does not encode health.
    luminance = (-0.45 * nx + 0.62 * ny + 0.64 * nz) / norm
    return depth, material, luminance


def _ear_hit(
    sx: float, sy: float, cosine: float, sine: float, facet: tuple,
) -> Optional[Tuple[float, str, float]]:
    points = tuple((x * cosine + z * sine, y, z * cosine - x * sine)
                   for x, y, z in facet[:3])
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points
    denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denominator) < 1e-12:
        return None
    u = ((by - cy) * (sx - cx) + (cx - bx) * (sy - cy)) / denominator
    v = ((cy - ay) * (sx - cx) + (ax - cx) * (sy - cy)) / denominator
    w = 1.0 - u - v
    if min(u, v, w) < 0.0:
        return None
    return u * az + v * bz + w * cz, facet[3], 0.65 if ax < 0.0 else 0.25


@lru_cache(maxsize=MASCOT_CYCLE)
def _sculpture(phase: int) -> Tuple[Tuple[Optional[str], ...], ...]:
    # Rest pose is deliberately open-eyed and centered. A small continuous yaw,
    # breathing lift, and one short blink make a complete, nonrandom loop.
    theta = 2.0 * math.pi * phase / MASCOT_CYCLE
    yaw = 0.23 * math.sin(theta)
    cosine, sine = math.cos(yaw), math.sin(yaw)
    lift = 0.025 * (1.0 - math.cos(theta * 2.0))
    blink = phase in (57, 58)
    eye_height = 0.025 if blink else 0.105
    eyes = (
        (-0.335, 0.405, 0.532, 0.091, eye_height, 0.065, "hole"),
        (0.335, 0.405, 0.532, 0.091, eye_height, 0.065, "hole"),
    )
    glints = () if blink else (
        (-0.365, 0.443, 0.594, 0.024, 0.027, 0.024, "glint"),
        (0.305, 0.443, 0.594, 0.024, 0.027, 0.024, "glint"),
    )
    shapes = _ELLIPSOIDS + eyes + glints
    pixels = []
    for py in range(MASCOT_HEIGHT * 4):
        sy = 1.36 - (py + 0.5) * 2.13 / (MASCOT_HEIGHT * 4) - lift
        row = []
        for px in range(MASCOT_WIDTH * 2):
            sx = -1.25 + (px + 0.5) * 2.50 / (MASCOT_WIDTH * 2)
            winner = None
            for shape in shapes:
                hit = _ellipsoid_hit(sx, sy, cosine, sine, shape)
                if hit is not None and (winner is None or hit[0] > winner[0]):
                    winner = hit
            for facet in _EARS:
                hit = _ear_hit(sx, sy, cosine, sine, facet)
                if hit is not None and (winner is None or hit[0] > winner[0]):
                    winner = hit
            role = None
            if winner is not None:
                _depth, material, luminance = winner
                if material != "hole":
                    if material == "glint":
                        role = "detail"
                    elif material == "inner":
                        role = "shadow"
                    elif material == "snout":
                        role = "detail" if luminance > 0.20 else "shadow"
                    elif luminance > 0.65:
                        role = "highlight"
                    elif luminance < 0.05:
                        role = "shadow"
                    else:
                        role = "brand"
            elif -0.76 < sy + lift < -0.64 and abs(sx) < 0.65:
                role = "structure"
            row.append(role)
        pixels.append(tuple(row))
    return tuple(pixels)


def _spans(cells: Tuple[Tuple[str, str], ...]) -> Tuple[VisualSpan, ...]:
    spans = []
    for glyph, role in cells:
        if spans and spans[-1].role == role:
            previous = spans[-1]
            spans[-1] = VisualSpan(previous.text + glyph, role)
        else:
            spans.append(VisualSpan(glyph, role))
    return tuple(spans)


def _ascii_frame(phase: int) -> MascotFrame:
    eye = "-" if phase in (57, 58) else "o"
    lines = (
        "       /\\        /\\",
        "      /  \\______/  \\",
        "     /              \\",
        "    |   %s  __  %s     |" % (eye, eye),
        "    |     (oo)       |",
        "     \\    \\__/      /",
        "      '------------'",
    )
    return MascotFrame(tuple((VisualSpan(line.ljust(MASCOT_WIDTH), "brand"),)
                             for line in lines))


@lru_cache(maxsize=MASCOT_CYCLE * 2)
def _render_frame(phase: int, style: str, unicode_enabled: bool) -> MascotFrame:
    if style == "ascii" or not unicode_enabled:
        return _ascii_frame(phase)
    pixels = _sculpture(phase)
    rows = []
    for cy in range(MASCOT_HEIGHT):
        cells = []
        for cx in range(MASCOT_WIDTH):
            mask = 0
            votes = {}
            for dy in range(4):
                for dx in range(2):
                    py, px = cy * 4 + dy, cx * 2 + dx
                    role = pixels[py][px]
                    if role is None:
                        continue
                    if style == "outline" and role != "structure":
                        neighbors = (pixels[ny][nx]
                                     if 0 <= ny < MASCOT_HEIGHT * 4
                                     and 0 <= nx < MASCOT_WIDTH * 2 else None
                                     for nx, ny in ((px - 1, py), (px + 1, py),
                                                    (px, py - 1), (px, py + 1)))
                        if all(neighbor == role for neighbor in neighbors):
                            continue
                    mask |= _BRAILLE_BITS[dy][dx]
                    votes[role] = votes.get(role, 0) + 1
            # Material boundaries use the dominant ink in each cell. Ties are
            # stable because subcells are traversed in a fixed order.
            role = max(votes, key=votes.get) if votes else "structure"
            cells.append((chr(0x2800 + mask) if mask else " ", role))
        rows.append(_spans(tuple(cells)))
    return MascotFrame(tuple(rows))


def render_mascot(
    phase: int, style: str = "sculpted", unicode_enabled: bool = True,
) -> MascotFrame:
    """Return one immutable 28×7 frame; phase wraps in a bounded 96-frame loop.

    `phase=0` is the motion-off pose. Braille uses one display cell per glyph;
    explicit ASCII works with fonts that lack it, even in a UTF-8 terminal.
    """

    if type(phase) is not int:
        raise ValueError("mascot phase must be an integer")
    if style not in MASCOT_STYLES:
        raise ValueError("unknown mascot style")
    if type(unicode_enabled) is not bool:
        raise ValueError("unicode_enabled must be a bool")
    return _render_frame(phase % MASCOT_CYCLE, style, unicode_enabled)


def wordmark_lines(style: str = "compact", unicode_enabled: bool = True) -> Tuple[str, ...]:
    """Return typographic artwork without changing the user's terminal font."""

    if style == "compact":
        return ("P O R K Y H U D",)
    if style == "outline":
        if unicode_enabled:
            return ("╭─ P O R K Y H U D ─╮", "╰─ MACOS OBSERVATORY ─╯")
        return ("+- P O R K Y H U D -+", "+- MACOS OBSERVATORY -+")
    raise ValueError("unknown wordmark style")
