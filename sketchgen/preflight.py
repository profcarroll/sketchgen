"""preflight.py — the p5 name a sketch hid from itself, said in one sentence.

Seven of the eleven crashing attempts the gate has ever recorded are one bug,
and it is always the same shape: the model declares a variable whose name is a
p5.js global, the declaration hides the library's own binding, and then the
sketch calls it. ``for (let line of lines) { … line(x1, y1, x2, y2); }`` ends as
``line is not a function``. The gate reports that console line faithfully,
because that is all the browser said, and ``qwen3-coder`` does not read it as a
name collision: job 16 spent all three attempts on ``line`` and job 34 two on
``scale``, each attempt rewriting the drawing code around a bug that was in the
``for`` header.

So this module is not a linter and does not want to be one. It is a scan that
runs *before* the evidence goes back to the executor and adds one sentence:
*your variable ``line`` hides p5's ``line()`` function; rename it*. The gate
stays the authority on whether a sketch is good; the pre-flight only explains
the error the gate already found.

Two decisions worth writing down rather than leaving in the code:

**A hidden function is only reported when the sketch calls it.** ``let hue =
map(…)`` hides p5's ``hue()`` and is completely harmless in a sketch that never
calls ``hue()`` — all three fixture sketches contain exactly such a declaration
next to the one that actually crashed them. Reporting it would hand the repair
two sentences, one of them wrong, and the repair has three attempts to spend.
A *variable* global (``width``, ``key``, ``PI``) has no call to wait for: hiding
it silently substitutes your value for p5's everywhere below, so it is reported
on sight.

**Regex, not a parser.** There is no JS parser in the standard library and this
system has no dependencies. Comments and string literals are blanked out first
(spaces, newlines kept, so line numbers survive) and the declarations are found
with a small hand-written walker. It therefore misses things — a declaration
inside a template-literal expression, a name introduced by a regex literal that
looks like code — and that is the right way round: a scan that says nothing
costs one ordinary repair attempt, a scan that invents a finding costs the
attempt *and* misdirects the next one. Property access (``line.x1``), object
keys (``{ line: 3 }``) and ``obj.line`` are never declarations and are never
reported.

Python 3.12, stdlib only. Nothing here touches the network, the database or the
filesystem beyond reading one ``sketch.js``.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "P5_FUNCTIONS",
    "P5_GLOBALS",
    "P5_VARIABLES",
    "SKETCH_FILE",
    "Shadow",
    "evidence_lines",
    "scan",
    "scan_dir",
]

#: The file the executor writes the sketch into (sketchgen/executor.py).
SKETCH_FILE = "sketch.js"

# The two lists below are hand-copied from the p5.js reference, and they are
# deliberately not the whole API: they are the names a 3k-token sketch actually
# reaches for, which is also where the collisions happen. Adding the full
# reference would protect names no generated sketch has ever used while making
# every ordinary identifier a candidate for a false finding.

#: p5 globals that are functions. Hiding one is only a problem once the sketch
#: calls it — see the module docstring.
P5_FUNCTIONS = frozenset({
    "line", "rect", "ellipse", "circle", "point", "arc", "triangle", "quad",
    "scale", "rotate", "translate", "push", "pop", "fill", "stroke", "noFill",
    "noStroke", "color", "text", "textSize", "background", "random", "noise",
    "map", "constrain", "lerp", "dist", "abs", "sin", "cos", "tan", "atan2",
    "min", "max", "floor", "ceil", "round", "sqrt", "pow", "createCanvas",
    "resizeCanvas", "redraw", "loop", "noLoop", "frameRate", "millis", "image",
    "loadImage", "createGraphics", "beginShape", "endShape", "vertex",
    "curveVertex", "bezier", "curve", "strokeWeight", "blendMode", "lights",
    "ambientLight", "directionalLight", "pointLight", "sphere", "box", "plane",
    "cone", "cylinder", "torus", "camera", "perspective", "ortho", "lerpColor",
    "red", "green", "blue", "alpha", "hue", "saturation", "brightness",
    "colorMode", "angleMode", "rectMode", "ellipseMode", "imageMode",
    "textAlign", "textFont", "textWidth", "pixelDensity", "loadPixels",
    "updatePixels", "get", "set", "filter", "shader", "createVector",
    "createShader", "keyIsDown", "saveCanvas", "print",
})

#: p5 globals that are values. Hiding one needs no call to go wrong.
P5_VARIABLES = frozenset({
    "width", "height", "mouseX", "mouseY", "pmouseX", "pmouseY",
    "mouseIsPressed", "frameCount", "deltaTime", "key", "keyCode",
    "keyIsPressed", "windowWidth", "windowHeight", "pixels", "touches",
    "PI", "TWO_PI", "HALF_PI", "QUARTER_PI", "TAU", "DEGREES", "RADIANS",
    "WEBGL", "P2D", "CENTER", "CORNER", "HSB", "RGB", "HSL",
})

#: Every name the scan protects.
P5_GLOBALS = P5_FUNCTIONS | P5_VARIABLES


@dataclass(frozen=True)
class Shadow:
    """One p5 name a sketch declared over, and where it did it."""

    name: str
    line: int
    kind: str  # "function" or "variable"

    @property
    def sentence(self) -> str:
        """The one sentence the repair attempt is given about this name."""
        if self.kind == "function":
            return (
                f"your variable `{self.name}` hides p5's `{self.name}()` "
                f"function; rename it"
            )
        return f"your variable `{self.name}` hides p5's `{self.name}`; rename it"


# ---------------------------------------------------------------------------
# Blanking comments and strings
# ---------------------------------------------------------------------------

_QUOTES = "'\"`"


def _blank(source: str) -> str:
    """``source`` with comments and string literals replaced by spaces.

    Newlines are kept and every other character becomes a space, so the result
    has the same length and the same line breaks as the input: an offset in the
    blanked text is the same offset in the original, and a line number computed
    from one is the line number in the other. A state machine rather than a
    regex, because a ``//`` inside a string and a quote inside a comment both
    have to be inert and no single regex gets both right.
    """
    out = list(source)
    i = 0
    end = len(source)
    while i < end:
        char = source[i]
        if char == "/" and i + 1 < end and source[i + 1] == "/":
            while i < end and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if char == "/" and i + 1 < end and source[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < end:
                if source[i] == "*" and i + 1 < end and source[i + 1] == "/":
                    out[i] = out[i + 1] = " "
                    i += 2
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        if char in _QUOTES:
            quote = char
            out[i] = " "
            i += 1
            while i < end:
                if source[i] == "\\":
                    out[i] = " "
                    if i + 1 < end and source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if source[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                # A single- or double-quoted string cannot span a line; a
                # template literal can, and either way the newline is kept.
                if source[i] == "\n" and quote != "`":
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Finding the declarations
# ---------------------------------------------------------------------------

_DECLARATION_RE = re.compile(r"(?<![\w$.])(?:let|const|var)\b")
_FUNCTION_RE = re.compile(
    r"(?<![\w$.])function\s*\*?\s*([A-Za-z_$][\w$]*)?\s*\(([^()]*)\)"
)
_ARROW_PARENS_RE = re.compile(r"\(([^()]*)\)\s*=>")
_ARROW_BARE_RE = re.compile(r"(?<![\w$.)\]])([A-Za-z_$][\w$]*)\s*=>")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")

#: Characters that, as the last thing on a line, mean the statement continues.
#: Used only to stop a declaration walk running away when the sketch omits its
#: semicolons; JavaScript's own rule is longer, and this is the cheap half of it
#: that matters here.
_CONTINUES = set(",=+-*/%&|?:([{<>!^~\\")

_PAIRS = {")": "(", "]": "[", "}": "{"}


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _line_of(starts: list[int], position: int) -> int:
    return bisect_right(starts, position)


def _identifier_at(source: str, position: int) -> tuple[str, int]:
    """The identifier beginning at ``position`` and the offset just past it."""
    match = _IDENTIFIER_RE.match(source, position)
    if match is None:  # pragma: no cover - callers check the first character
        return "", position + 1
    return match.group(0), match.end()


def _previous_nonspace(source: str, position: int) -> str:
    index = position - 1
    while index >= 0 and source[index] in " \t":
        index -= 1
    return source[index] if index >= 0 else ""


def _next_nonspace(source: str, position: int, limit: int) -> str:
    index = position
    while index < limit and source[index] in " \t\n\r":
        index += 1
    return source[index] if index < limit else ""


def _matching(source: str, position: int) -> int:
    """Offset of the bracket closing the one at ``position``, or end of source."""
    depth = 0
    for index in range(position, len(source)):
        char = source[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index
    return len(source)


def _binding_names(source: str, start: int, end: int) -> list[tuple[str, int]]:
    """Names bound by a destructuring pattern or a parameter list.

    ``{ a: b }`` binds ``b`` and not ``a``, ``{ a = width }`` binds ``a`` and
    not ``width``, ``[x, ...rest]`` binds both. The rule is small on purpose:
    an identifier counts unless a ``:`` follows it (it is the key) or a ``=``
    or ``.`` precedes it (it is part of a default or a property path).
    """
    found: list[tuple[str, int]] = []
    for match in _IDENTIFIER_RE.finditer(source, start, end):
        if _next_nonspace(source, match.end(), end) == ":":
            continue
        if _previous_nonspace(source, match.start()) in ("=", "."):
            continue
        found.append((match.group(0), match.start()))
    return found


def _declared_names(source: str) -> list[tuple[str, int]]:
    """Every name the blanked ``source`` declares, as ``(name, offset)``.

    ``let``/``const``/``var`` declarator lists (including the header of a
    ``for … of`` / ``for … in`` and comma-separated declarators), function
    declarations and their parameters, and arrow-function parameters. Nothing
    else: an assignment to an existing name shadows nothing.
    """
    found: list[tuple[str, int]] = []
    length = len(source)

    for keyword in _DECLARATION_RE.finditer(source):
        index = keyword.end()
        depth = 0
        expect_name = True
        while index < length:
            char = source[index]
            if char in "[{" and expect_name and depth == 0:
                close = _matching(source, index)
                found.extend(_binding_names(source, index + 1, close))
                index = close + 1
                expect_name = False
                continue
            if char in "([{":
                depth += 1
                index += 1
                expect_name = False
                continue
            if char in ")]}":
                if depth == 0:
                    break  # the ')' of a for-header ends the declaration
                depth -= 1
                index += 1
                continue
            if char == ";" and depth == 0:
                break
            if char == "," and depth == 0:
                expect_name = True
                index += 1
                continue
            if char == "\n":
                if depth == 0 and _previous_nonspace(source, index) not in _CONTINUES:
                    break  # a statement the sketch ended without a semicolon
                index += 1
                continue
            if char.isspace():
                index += 1
                continue
            if expect_name and (char.isalpha() or char in "_$"):
                name, index = _identifier_at(source, index)
                found.append((name, index - len(name)))
                expect_name = False
                continue
            expect_name = False
            index += 1

    for match in _FUNCTION_RE.finditer(source):
        if match.group(1):
            found.append((match.group(1), match.start(1)))
        found.extend(_binding_names(source, match.start(2), match.end(2)))

    for match in _ARROW_PARENS_RE.finditer(source):
        found.extend(_binding_names(source, match.start(1), match.end(1)))

    for match in _ARROW_BARE_RE.finditer(source):
        found.append((match.group(1), match.start(1)))

    return found


def _is_called(source: str, name: str, declared_at: int) -> bool:
    """True when ``name`` is called somewhere other than its own declaration.

    ``obj.line(…)`` is a method on somebody else's object and is not a call to
    the shadowed name, so a ``.`` (or a further name character) before the name
    disqualifies the match.
    """
    pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
    for match in pattern.finditer(source):
        if match.start() != declared_at:
            return True
    return False


def scan(source: str) -> list[Shadow]:
    """Every p5 global this sketch declares over, in line order.

    A function global is reported only when the sketch also calls it — that call
    is the crash the gate saw. A variable global is reported on sight. Findings
    are deduplicated by ``(name, line)``, so one name declared once inside a
    loop body is one finding however many times the loop runs.
    """
    blanked = _blank(source)
    starts = _line_starts(blanked)
    seen: set[tuple[str, int]] = set()
    found: list[Shadow] = []

    for name, position in _declared_names(blanked):
        if name not in P5_GLOBALS:
            continue
        kind = "function" if name in P5_FUNCTIONS else "variable"
        if kind == "function" and not _is_called(blanked, name, position):
            continue
        line = _line_of(starts, position)
        if (name, line) in seen:
            continue
        seen.add((name, line))
        found.append(Shadow(name=name, line=line, kind=kind))

    found.sort(key=lambda shadow: (shadow.line, shadow.name))
    return found


def scan_dir(sketch_dir) -> list[Shadow]:
    """:func:`scan` over ``<sketch_dir>/sketch.js``; empty when there is none.

    An attempt that never got as far as a sketch file (a malformed executor
    response, a directory the gate refused) is not an error here: there is
    simply nothing to say about it.
    """
    path = Path(sketch_dir) / SKETCH_FILE
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return scan(source)


def evidence_lines(shadows) -> list[str]:
    """The findings as the lines that go into the repair attempt's evidence."""
    return [
        f"preflight: {shadow.sentence} ({SKETCH_FILE} line {shadow.line})"
        for shadow in shadows
    ]
