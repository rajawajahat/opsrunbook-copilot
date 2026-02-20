"""
Iteration 7 – Stacktrace parser and path normalizer.

Extracts structured frames from Python and Node.js stacktrace formats,
normalizes paths by stripping runtime prefixes, and filters noise
(site-packages, node_modules, etc.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── Runtime path prefixes to strip ────────────────────────────────
_STRIP_PREFIXES = [
    "/var/task/",
    "/usr/src/app/",
    "/app/",
    "/opt/python/",
    "/opt/",
    re.compile(r"/home/runner/work/[^/]+/[^/]+/"),
    re.compile(r"/tmp/[a-f0-9-]+/"),
]

# ── Noise path patterns to ignore ────────────────────────────────
_NOISE_PATTERNS = [
    re.compile(r"site-packages/"),
    re.compile(r"node_modules/"),
    re.compile(r"\.venv/"),
    re.compile(r"dist-packages/"),
    re.compile(r"<frozen "),
    re.compile(r"<string>"),
    re.compile(r"<module>"),
    re.compile(r"importlib"),
    re.compile(r"_bootstrap"),
    re.compile(r"__pycache__"),
    re.compile(r"lib/python\d"),
]

MAX_APP_FRAMES = 5


@dataclass
class TraceFrame:
    raw_path: str
    normalized_path: str
    line: int | None
    column: int | None = None
    function: str = ""

    def to_dict(self) -> dict:
        return {
            "raw_path": self.raw_path,
            "normalized_path": self.normalized_path,
            "line": self.line,
            "column": self.column,
            "function": self.function,
        }


def normalize_path(raw: str) -> str:
    """Strip runtime prefixes from a file path."""
    result = raw.strip()
    for prefix in _STRIP_PREFIXES:
        if isinstance(prefix, str):
            if result.startswith(prefix):
                result = result[len(prefix):]
        else:
            result = prefix.sub("", result, count=1)
    # Remove leading ./
    if result.startswith("./"):
        result = result[2:]
    return result


def _is_noise(path: str) -> bool:
    for pattern in _NOISE_PATTERNS:
        if pattern.search(path):
            return True
    return False


# ── Python trace patterns ─────────────────────────────────────────
# File "/var/task/handler.py", line 42, in lambda_handler
_PY_FRAME = re.compile(
    r'File "([^"]+)",\s+line (\d+)(?:,\s+in (\S+))?'
)

# ── Node.js trace patterns ────────────────────────────────────────
# at functionName (/path/to/file.js:10:5)
# at /path/to/file.js:10:5
_NODE_FRAME = re.compile(
    r'at\s+(?:(\S+)\s+)?\(?([^():]+):(\d+):(\d+)\)?'
)

# ── Generic path:line ─────────────────────────────────────────────
_GENERIC_PATHLINE = re.compile(
    r'([\w./_-]+\.\w{1,5}):(\d+)'
)


def parse_frames(text: str) -> list[TraceFrame]:
    """Extract all trace frames from text containing stacktrace output."""
    frames: list[TraceFrame] = []
    seen_paths: set[str] = set()

    # Python frames
    for m in _PY_FRAME.finditer(text):
        raw = m.group(1)
        norm = normalize_path(raw)
        key = f"{norm}:{m.group(2)}"
        if key not in seen_paths:
            seen_paths.add(key)
            frames.append(TraceFrame(
                raw_path=raw,
                normalized_path=norm,
                line=int(m.group(2)),
                function=m.group(3) or "",
            ))

    # Node frames
    for m in _NODE_FRAME.finditer(text):
        raw = m.group(2)
        norm = normalize_path(raw)
        key = f"{norm}:{m.group(3)}"
        if key not in seen_paths:
            seen_paths.add(key)
            frames.append(TraceFrame(
                raw_path=raw,
                normalized_path=norm,
                line=int(m.group(3)),
                column=int(m.group(4)),
                function=m.group(1) or "",
            ))

    # Generic fallback for path:line patterns
    if not frames:
        for m in _GENERIC_PATHLINE.finditer(text):
            raw = m.group(1)
            norm = normalize_path(raw)
            key = f"{norm}:{m.group(2)}"
            if key not in seen_paths:
                seen_paths.add(key)
                frames.append(TraceFrame(
                    raw_path=raw,
                    normalized_path=norm,
                    line=int(m.group(2)),
                ))

    return frames


def extract_app_frames(text: str) -> list[TraceFrame]:
    """Parse frames, filter noise, return top N application frames."""
    all_frames = parse_frames(text)
    app_frames = [f for f in all_frames if not _is_noise(f.normalized_path)]
    return app_frames[:MAX_APP_FRAMES]
