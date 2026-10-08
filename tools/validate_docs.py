#!/usr/bin/env python3
"""Validate PorkyHUD's tracked-file map and local documentation contracts."""

from __future__ import annotations

import argparse
import ast
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import unicodedata
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit


MAP_HEADING = "## Complete tracked-file map"
SUCCESS_LINE = "Repository documentation validation passed."
PYTHON_ANCHOR_RE = re.compile(
    r"`([^`\r\n]+\.py)::([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`",
)
REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^\s*\[([^\]\r\n]+)\]:\s*(<[^>]+>|\S+)",
)
REFERENCE_USE_RE = re.compile(
    r"!?\[([^\]\r\n]+)\]\[([^\]\r\n]*)\]",
)
EXPECTED_DIRECT_MODES = {
    "1": "balanced",
    "2": "compute",
    "3": "thermals",
    "4": "io",
    "5": "processes",
    "6": "cinema",
    "7": "insights", "8": "devices", "9": "network", "0": "connections",
    "i": "system",
}


def _without_fenced_code(markdown_text: str) -> str:
    """Replace fenced regions with blank lines while retaining line positions."""

    output: List[str] = []
    fence: Optional[Tuple[str, int]] = None
    for line in markdown_text.splitlines(keepends=True):
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if match is not None:
            marker = match.group(1)
            marker_kind = marker[0]
            if fence is None:
                fence = (marker_kind, len(marker))
            elif fence[0] == marker_kind and len(marker) >= fence[1]:
                fence = None
            output.append("\n" if line.endswith("\n") else "")
        elif fence is None:
            output.append(line)
        else:
            output.append("\n" if line.endswith("\n") else "")
    return "".join(output)


def _validate_repo_path(path: str) -> str:
    if not path or "\\" in path or PurePosixPath(path).is_absolute():
        raise ValueError("malformed repository path: %r" % path)
    if any(unicodedata.category(character).startswith("C") for character in path):
        raise ValueError("malformed repository path: %r" % path)
    parts = PurePosixPath(path).parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("malformed repository path: %r" % path)
    normalized = posixpath.normpath(path)
    if path != normalized or path.startswith("./") or "//" in path:
        raise ValueError("non-normalized repository path: %r" % path)
    return path


def tracked_files(root: Path) -> Tuple[str, ...]:
    """Return the exact tracked plus unignored source inventory for ``root``."""

    try:
        result = subprocess.run(
            [
                "git", "-C", os.fspath(root), "ls-files", "-z", "--cached",
                "--others", "--exclude-standard",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("could not read Git inventory") from exc
    if result.returncode != 0:
        raise ValueError("root is not a readable Git worktree")
    try:
        decoded = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Git inventory is not valid UTF-8") from exc
    if decoded and not decoded.endswith("\0"):
        raise ValueError("malformed NUL-delimited Git inventory")
    paths = [_validate_repo_path(value) for value in decoded.split("\0") if value]
    folded = {}
    for path in paths:
        key = path.casefold()
        previous = folded.get(key)
        if previous is not None and previous != path:
            raise ValueError(
                "case-conflicting repository paths: %s and %s" % (previous, path),
            )
        folded[key] = path
    return tuple(sorted(paths))


def mapped_files(agent_text: str) -> Tuple[str, ...]:
    """Parse the strict body of AGENTS.md's canonical tracked-file table."""

    lines = agent_text.splitlines()
    headings = [index for index, line in enumerate(lines) if line == MAP_HEADING]
    if len(headings) != 1:
        raise ValueError("expected one exact complete tracked-file map heading")
    start = headings[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    section = lines[start:end]
    header_index: Optional[int] = None
    for index, line in enumerate(section):
        if re.match(r"^\|\s*Path\s*\|", line):
            header_index = index
            break
    if header_index is None or header_index + 1 >= len(section):
        raise ValueError("missing tracked-file map table")
    if not re.match(r"^\|(?:\s*:?-{3,}:?\s*\|){3}\s*$", section[header_index + 1]):
        raise ValueError("malformed tracked-file map delimiter")
    paths: List[str] = []
    for line in section[header_index + 2:]:
        if not line.strip():
            if paths:
                break
            continue
        if not line.lstrip().startswith("|"):
            if paths:
                break
            continue
        cells = line.split("|")[1:-1]
        if len(cells) != 3:
            raise ValueError("malformed tracked-file map row")
        match = re.fullmatch(r"\s*`([^`]+)`\s*", cells[0])
        if match is None:
            raise ValueError("malformed tracked-file map row")
        path = _validate_repo_path(match.group(1))
        if path in paths:
            raise ValueError("duplicate mapped path: %s" % path)
        for previous in paths:
            if previous.casefold() == path.casefold():
                raise ValueError(
                    "case-conflicting mapped paths: %s and %s" % (previous, path),
                )
        paths.append(path)
    if not paths:
        raise ValueError("tracked-file map has no body rows")
    return tuple(paths)


def symbol_anchors(markdown_text: str) -> Tuple[Tuple[str, str], ...]:
    """Return sorted Python path/qualname anchors outside fenced code."""

    anchors = {
        (_validate_repo_path(match.group(1)), match.group(2))
        for match in PYTHON_ANCHOR_RE.finditer(_without_fenced_code(markdown_text))
    }
    return tuple(sorted(anchors))


def _read_text(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("could not read %s" % relative) from exc


def validate_tracked_map(root: Path) -> List[str]:
    """Report inventory/map mismatches in deterministic order."""

    inventory = set(tracked_files(root))
    mapped = set(mapped_files(_read_text(root, "AGENTS.md")))
    errors = [
        "AGENTS map path is not tracked: %s" % path
        for path in sorted(mapped - inventory)
    ]
    errors.extend(
        "tracked file missing from AGENTS map: %s" % path
        for path in sorted(inventory - mapped)
    )
    return sorted(errors)


def _exact_path_status(root: Path, relative: str) -> Tuple[bool, bool]:
    """Return (exists_exactly, exists_with_different_case)."""

    current = root
    mismatch = False
    for part in PurePosixPath(relative).parts:
        try:
            names = os.listdir(current)
        except OSError:
            return False, mismatch
        if part in names:
            current = current / part
            continue
        folded = [name for name in names if name.casefold() == part.casefold()]
        if folded:
            mismatch = True
        return False, mismatch
    return current.exists() or current.is_symlink(), mismatch


def _python_symbols(source: str, path: str) -> set[str]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise ValueError("could not parse Python source: %s" % path) from exc
    symbols: set[str] = set()

    def assignment_names(node: ast.AST) -> Iterable[str]:
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id

    def class_members(node: ast.ClassDef, prefix: str) -> None:
        symbols.add(prefix)
        for child in node.body:
            for name in assignment_names(child):
                symbols.add(prefix + "." + name)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(prefix + "." + child.name)
            elif isinstance(child, ast.ClassDef):
                class_members(child, prefix + "." + child.name)

    for node in tree.body:
        for name in assignment_names(node):
            symbols.add(name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            class_members(node, node.name)
    return symbols


def validate_symbol_anchors(root: Path) -> List[str]:
    """Validate mapped-path existence and AGENTS-only Python AST anchors."""

    agents = _read_text(root, "AGENTS.md")
    errors: List[str] = []
    for path in mapped_files(agents):
        exact, mismatch = _exact_path_status(root, path)
        if not exact:
            label = "mapped path case mismatch" if mismatch else "mapped path does not exist"
            errors.append("%s: %s" % (label, path))
    sources = {}
    for path, qualname in symbol_anchors(agents):
        exact, mismatch = _exact_path_status(root, path)
        if not exact:
            label = "Python anchor path case mismatch" if mismatch else "missing Python anchor path"
            errors.append("%s: %s" % (label, path))
            continue
        if path not in sources:
            sources[path] = _python_symbols(_read_text(root, path), path)
        if qualname not in sources[path]:
            errors.append("missing Python symbol anchor: %s::%s" % (path, qualname))
    return sorted(set(errors))


class _LinkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.destinations: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        for name, value in attrs:
            if name.lower() in ("href", "src") and value:
                self.destinations.append(value)


def _inline_destinations(markdown_text: str) -> Tuple[str, ...]:
    """Parse inline destinations while preserving balanced parentheses."""

    values: List[str] = []
    cursor = 0
    while True:
        marker = markdown_text.find("](", cursor)
        if marker < 0:
            break
        line_start = markdown_text.rfind("\n", 0, marker) + 1
        if markdown_text.rfind("[", line_start, marker) < 0:
            cursor = marker + 2
            continue
        start = marker + 2
        while start < len(markdown_text) and markdown_text[start] in " \t":
            start += 1
        if start >= len(markdown_text):
            break
        if markdown_text[start] == "<":
            end = markdown_text.find(">", start + 1)
            if end >= 0:
                values.append(markdown_text[start:end + 1])
                cursor = end + 1
                continue
        depth = 0
        end = start
        while end < len(markdown_text):
            character = markdown_text[end]
            if character == "\\" and end + 1 < len(markdown_text):
                end += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    break
                depth -= 1
            elif character.isspace() and depth == 0:
                break
            end += 1
        if end > start:
            values.append(markdown_text[start:end])
        cursor = max(marker + 2, end + 1)
    return tuple(values)


def _reference_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _markdown_links(markdown_text: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    visible = _without_fenced_code(markdown_text)
    definitions = {
        _reference_label(match.group(1)): match.group(2)
        for match in REFERENCE_DEFINITION_RE.finditer(visible)
    }
    values = list(_inline_destinations(visible))
    values.extend(definitions.values())
    undefined = []
    for match in REFERENCE_USE_RE.finditer(visible):
        label = _reference_label(match.group(2) or match.group(1))
        if label not in definitions:
            undefined.append(label)
    parser = _LinkHTMLParser()
    parser.feed(visible)
    values.extend(parser.destinations)
    destinations = tuple(
        value[1:-1] if value.startswith("<") and value.endswith(">") else value
        for value in values
    )
    return destinations, tuple(sorted(set(undefined)))


def _diagnostic_link_text(value: str) -> str:
    decoded = unquote(value)
    if (not value or len(value) > 160 or "\\" in decoded
            or decoded.startswith("/")
            or any(unicodedata.category(character).startswith("C")
                   for character in decoded)):
        return "<unsafe>"
    return value


def _resolved_relative(source: str, destination: str) -> Tuple[Optional[str], Optional[str]]:
    decoded = unquote(destination)
    if any(unicodedata.category(character).startswith("C") for character in decoded):
        return None, "unsafe control character"
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc:
        return None, None
    if not parsed.path:
        return None, None
    if "\\" in parsed.path or parsed.path.startswith("/"):
        return None, "outside repository"
    combined = PurePosixPath(source).parent.joinpath(PurePosixPath(parsed.path))
    parts: List[str] = []
    escaped = False
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            else:
                escaped = True
        else:
            parts.append(part)
    if escaped or not parts:
        return None, "outside repository"
    return "/".join(parts), None


def validate_local_links(root: Path) -> List[str]:
    """Validate local targets from every Markdown file in the Git inventory."""

    inventory = set(tracked_files(root))
    errors: List[str] = []
    try:
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("could not resolve repository root") from exc
    for source in sorted(path for path in inventory if path.lower().endswith(".md")):
        destinations, undefined = _markdown_links(_read_text(root, source))
        errors.extend(
            "%s: undefined reference link: %s" % (
                source, _diagnostic_link_text(label),
            )
            for label in undefined
        )
        for raw_destination in destinations:
            diagnostic = _diagnostic_link_text(raw_destination)
            target, error = _resolved_relative(source, raw_destination)
            if error is not None:
                errors.append("%s: %s link target: %s" % (source, error, diagnostic))
                continue
            if target is None:
                continue
            exact, mismatch = _exact_path_status(root, target)
            if mismatch:
                errors.append("%s: link target case mismatch: %s" % (source, diagnostic))
                continue
            candidate = root / target
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root_real)
            except ValueError:
                errors.append("%s: symlink escape: %s" % (source, diagnostic))
                continue
            if not exact or target not in inventory:
                errors.append("%s: untracked link target: %s" % (source, diagnostic))
    return sorted(set(errors))


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise ValueError("missing Python assignment: porkyhud.py::%s" % name)


def _function_node(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ValueError("missing Python function: porkyhud.py::%s" % name)


def _ord_controls(node: ast.AST) -> set[str]:
    return {
        call.args[0].value
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name) and call.func.id == "ord"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and len(call.args[0].value) == 1
    }


def _attribute_names(node: ast.AST, owner: str) -> set[str]:
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name) and child.value.id == owner
    }


def validate_control_contract(root: Path) -> List[str]:
    """Check semantic shortcut groups across their five user-facing surfaces."""

    errors: List[str] = []
    required = ("porkyhud.py", "PorkyHUD.command", "README.md")
    content = {}
    for path in required:
        exact, _ = _exact_path_status(root, path)
        if not exact:
            errors.append("control contract path does not exist: %s" % path)
            content[path] = ""
        else:
            content[path] = _read_text(root, path)
    source = content["porkyhud.py"]
    try:
        tree = ast.parse(source, filename="porkyhud.py")
        direct_modes = _literal_assignment(tree, "DIRECT_MODE_KEYS")
    except (SyntaxError, ValueError) as exc:
        errors.append(str(exc))
        direct_modes = None
        tree = ast.parse("")
    if direct_modes != EXPECTED_DIRECT_MODES:
        errors.append("DIRECT_MODE_KEYS does not match the canonical direct-mode mapping")
    try:
        handler = _function_node(tree, "_hud_impl")
    except ValueError as exc:
        errors.append(str(exc))
        handler = ast.parse("")
    controls = _ord_controls(handler)
    for control in ("h", "l", "a", "m", "r", "d", "s"):
        if control not in controls:
            errors.append("input handler missing semantic control: %s" % control)
    curses_attributes = _attribute_names(handler, "curses")
    for attribute in ("KEY_HOME", "KEY_END"):
        if attribute not in curses_attributes:
            errors.append("input handler missing semantic control: curses.%s" % attribute)
    direct_references = sum(
        isinstance(node, ast.Name) and node.id == "DIRECT_MODE_KEYS"
        for node in ast.walk(handler)
    )
    if direct_references < 2:
        errors.append("input handler does not route the canonical direct modes")
    try:
        help_node = _function_node(tree, "draw_help_overlay")
    except ValueError as exc:
        errors.append(str(exc))
        help_node = ast.parse("")
    help_text = "\n".join(
        node.value for node in ast.walk(help_node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    help_tokens = (
        "1–0 / i       choose view · d all tools",
        "t / s         theme / sculpted, outline, ASCII artwork",
        "Tab/Shift-Tab next/previous view · l next",
        "/             find processes or inventory",
        "f             TCP: all / listeners / established / other",
        "a             motion: off / calm / vivid",
        "PgUp/PgDn/Home/End  page or jump to either end",
        "6  Cinema     select wide Cinema mode",
    )
    for token in help_tokens:
        if token not in help_text:
            errors.append("in-app help missing semantic control: %s" % token)
    readme_tokens = (
        "`1`–`5`", "`6`", "supported", "motion", "`Home` / `End`",
        "temporary", "`h`", "`q`",
    )
    for token in readme_tokens:
        if token not in content["README.md"]:
            errors.append("README controls missing semantic token: %s" % token)
    expected_short_copy = "Press h for current shortcuts."
    module_docstring = ast.get_docstring(tree, clean=False) or ""
    if module_docstring.strip().splitlines()[-1:] != [expected_short_copy]:
        errors.append("module shortcut copy must end with: %s" % expected_short_copy)
    if content["PorkyHUD.command"].count(expected_short_copy) != 1:
        errors.append("launcher shortcut copy must contain exactly: %s" % expected_short_copy)
    launcher_lower = content["PorkyHUD.command"].lower()
    if "Keys:" in module_docstring or "Keys:" in content["PorkyHUD.command"]:
        errors.append("module and launcher must not duplicate the shortcut table")
    if "press u" in launcher_lower:
        errors.append("launcher must route shortcut details through current help")
    return sorted(set(errors))


def _all_errors(root: Path) -> List[str]:
    errors: List[str] = []
    for validator in (
        validate_tracked_map,
        validate_symbol_anchors,
        validate_local_links,
        validate_control_contract,
    ):
        try:
            errors.extend(validator(root))
        except ValueError as exc:
            errors.append(str(exc))
    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate PorkyHUD repository documentation contracts.",
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root
    if not root.is_dir() or not (root / ".git").exists():
        # A linked worktree stores .git as a file, so existence is intentional.
        if not root.is_dir() or not (root / ".git").is_file():
            parser.error("--root must name a Git worktree")
    errors = _all_errors(root)
    if errors:
        for error in errors:
            print("ERROR " + error, file=sys.stderr)
        return 1
    print(SUCCESS_LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
