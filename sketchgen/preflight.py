"""preflight.py — what a sketch does to itself, said in one sentence each.

Two scans, one mechanism. The first is the p5 name a sketch hid from itself;
the second, added 2026-09-15, is the frame a sketch cannot afford to draw.
Both produce findings with a ``line`` and a ``sentence``, both go into the
evidence the next attempt reads, and neither is a verdict — the gate is still
the only thing that says yes or no.

THE SECOND SCAN, IN ONE PARAGRAPH. Job 166 (entry 165) and job 270 (entry 269)
each drew about 1,500 ``sphere()`` meshes and tens of thousands of
immediate-mode ``line()`` calls per frame in WEBGL. Both passed the gate; both
took minutes of gate time; both pinned about 4 GB of GPU buffers and took the
operator's laptop down when the Held page previewed them. ``gate/sketch_gate.py``
now measures that cost and fails ``frame_budget``, which is the authority. This
scan is the *cause* beside that symptom, in the same place and for the same
reason the shadow scan is: "1093 ms per frame" is a number, and
"``sphere()`` is called inside a loop inside ``draw()``" is a line to change.
Job 270 was written under an executor prompt that already carries a frame
budget in prose, so prose alone does not hold.

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
    "Cost",
    "Shadow",
    "evidence_lines",
    "scan",
    "scan_all",
    "scan_cost",
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


@dataclass(frozen=True)
class Cost:
    """One thing a sketch does per frame that a frame cannot pay for."""

    rule: str      # the name the gate's frame_budget check would blame
    name: str      # the call or construct that costs, e.g. "sphere"
    line: int
    sentence: str


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


# ---------------------------------------------------------------------------
# The cost scan
# ---------------------------------------------------------------------------
#
# Same regex-not-a-parser bargain as above, and the same bias: a scan that says
# nothing costs one ordinary repair attempt, a scan that invents a finding costs
# the attempt AND misdirects the next one. Every rule below was run over all 372
# attempt sketches on the node before it was allowed to stay, and the ones that
# fired on ordinary published work were narrowed until they did not.

#: p5's 3D primitives. Each one builds (or looks up) a lit mesh; at p5 1.11's
#: default detail a ``sphere()`` is 24x16 quads. One per particle per frame is
#: what job 166 and job 270 both did.
GEOMETRY_CALLS = ("sphere", "box", "cylinder", "cone", "torus", "ellipsoid")

#: In WEBGL these two are immediate mode: every call builds and uploads its own
#: vertex buffer. 19,493 ``line()`` calls a frame is 19,493 buffer allocations a
#: frame, which is time on the node and memory in a real browser tab.
IMMEDIATE_CALLS = ("line", "point")

#: Allocations that belong in ``setup()``. A ``createGraphics()`` in ``draw()``
#: is a new framebuffer sixty times a second, and ``loadImage()`` is an
#: asynchronous fetch started again on every frame.
ALLOCATING_CALLS = ("createGraphics", "createImage", "loadImage", "loadShader",
                    "createShader", "loadFont")

#: A full-canvas shader pass. Job 45 ran twelve of them per frame inside a loop
#: and took 504 s to pass the gate — the slowest run the node has recorded.
FILTER_CALL = "filter"

#: How many times a loop has to run before what is inside it is worth a
#: sentence. 300 is the WEBGL shape-call budget the executor prompt already
#: carries, so the scan and the prompt agree on the number rather than each
#: having its own. A loop whose bound cannot be read — ``things.length``, a
#: ``for … of``, anything computed — is treated as large, because in this corpus
#: that bound is a particle array and the count is the thing that got away.
MIN_ITERATIONS = 300

#: Only a literal or a plainly-initialised constant is resolved. Chasing
#: ``things.length`` back to the ``push()`` that filled it is a data-flow
#: analysis, and this module's bargain (see the docstring) is that it would
#: rather say nothing than guess: an unreadable bound stays unread.
_BOUND_RE = re.compile(r";\s*[A-Za-z_$][\w$]*\s*<=?\s*([^;]+);")
_LITERAL_RE = re.compile(r"^\s*(\d+)\s*$")

#: How a WEBGL sketch announces itself. p5 has no other way to ask for it, and
#: an instance-mode sketch spells it ``p.WEBGL``, so a receiver is allowed.
_WEBGL_RE = re.compile(r"(?<![\w$])WEBGL(?![\w$])")

_LOOP_RE = re.compile(r"(?<![\w$.])(for|while)\s*\(")
_DRAW_RE = re.compile(
    r"(?<![\w$.])(?:function\s+draw\s*\(|"          # function draw()
    r"(?:[\w$]+\s*\.\s*)?draw\s*=\s*(?:function|\()|"   # p.draw = function / = (
    r"draw\s*\(\s*\)\s*\{)"                          # draw() { — a class method
)

#: ``for (let j = i + 1; …)``: the all-pairs loop, written the way every
#: textbook writes it.
_PAIR_HEADER_RE = re.compile(
    r"(?:let|const|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*\+\s*1\b"
)
#: ``i < things.length``: the bound that names the array being walked. Only the
#: ``.length`` form counts as "the same array", because two loops over the same
#: plain integer are how every grid in this corpus is drawn and none of them is
#: an all-pairs loop.
_LENGTH_BOUND_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\.\s*length\b")


def _call_positions(blanked: str, name: str, receiver: bool = False) -> list[int]:
    """Offsets of every call to ``name``.

    A ``.`` before the name normally disqualifies it — ``obj.line(…)`` is
    somebody else's method. ``receiver=True`` allows exactly one identifier in
    front, which is how an instance-mode sketch spells p5's own calls
    (``p.sphere(2)``) and how a sketch draws into a ``createGraphics`` buffer.
    It is off for ``filter``, because every JavaScript array has one of those
    and reporting ``items.filter(…)`` would be a finding about working code.
    """
    if receiver:
        pattern = re.compile(r"(?<![\w$.])(?:[A-Za-z_$][\w$]*\s*\.\s*)?"
                             + re.escape(name) + r"\s*\(")
    else:
        pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
    return [m.end() - len(name) - 1 if receiver else m.start()
            for m in pattern.finditer(blanked)]


def _block_after(source: str, position: int) -> tuple[int, int]:
    """The span of the ``{…}`` block that follows ``position``.

    Used for both a function's body and a loop's. A loop written without braces
    (``for (…) line(a, b, c, d);``) gets the rest of that statement instead,
    which is the smallest honest answer and still catches the one-line form.
    """
    index = position
    end = len(source)
    while index < end and source[index] in " \t\n\r":
        index += 1
    if index < end and source[index] == "{":
        return index, _matching(source, index)
    stop = source.find(";", index)
    return index, (end if stop == -1 else stop)


def _draw_bodies(blanked: str) -> list[tuple[int, int]]:
    """The span of every ``draw()`` body in the sketch, global or instance mode."""
    bodies = []
    for match in _DRAW_RE.finditer(blanked):
        # Step over the parameter list to the body's opening brace. A class
        # method's regex already consumed its ``{``, so back up onto it.
        index = match.end()
        if blanked[match.end() - 1] == "{":
            index = match.end() - 1
        else:
            paren = blanked.find("(", match.end() - 1)
            if paren == -1:
                continue
            index = _matching(blanked, paren) + 1
            arrow = blanked.find("=>", index)
            if 0 <= arrow <= index + 3:
                index = arrow + 2
        start, stop = _block_after(blanked, index)
        if stop > start:
            bodies.append((start, stop))
    return bodies


def _loops_within(blanked: str, start: int, stop: int) -> list[tuple[int, int, int]]:
    """``(header_start, body_start, body_end)`` for every loop in a span."""
    loops = []
    for match in _LOOP_RE.finditer(blanked, start, stop):
        header = _matching(blanked, match.end() - 1)
        body_start, body_end = _block_after(blanked, header + 1)
        loops.append((match.start(), body_start, min(body_end, stop)))
    return loops


def _inside(position: int, spans) -> bool:
    return any(start <= position < stop for start, stop in spans)


def _const_value(blanked: str, name: str) -> int | None:
    """``const N = 1500`` anywhere in the sketch, or None."""
    match = re.search(
        r"(?<![\w$.])(?:let|const|var)\s+" + re.escape(name) + r"\s*=\s*(\d+)\b",
        blanked,
    )
    return int(match.group(1)) if match else None


def _iterations(blanked: str, header: int, body_start: int) -> int | None:
    """How many times this loop runs, when that can be read off its header."""
    match = _BOUND_RE.search(blanked, header, body_start)
    if match is None:
        return None
    bound = match.group(1).strip()
    literal = _LITERAL_RE.match(bound)
    if literal:
        return int(literal.group(1))
    if re.fullmatch(r"[A-Za-z_$][\w$]*", bound):
        return _const_value(blanked, bound)
    subtraction = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*-\s*(\d+)", bound)
    if subtraction:
        value = _const_value(blanked, subtraction.group(1))
        return None if value is None else value - int(subtraction.group(2))
    return None


def _runs_often(blanked: str, loops, position: int) -> bool:
    """Whether the loops around ``position`` run ``MIN_ITERATIONS`` times or more.

    Nested loops multiply: eight bands of twenty is a hundred and sixty, which
    is under the budget and stays quiet. One unreadable bound anywhere in the
    nest makes the whole product unreadable, and an unreadable count is treated
    as large.
    """
    product = 1
    for header, body_start, body_end in loops:
        if not body_start <= position < body_end:
            continue
        count = _iterations(blanked, header, body_start)
        if count is None:
            return True
        product *= count
    return product >= MIN_ITERATIONS


def _all_pairs(blanked: str, loops) -> list[int]:
    """Offsets of the inner loop of every all-pairs walk in ``loops``.

    Two shapes, both from real attempts: ``for (let j = i + 1; …)`` nested in a
    loop over ``i``, and two nested loops whose bounds are the *same array's*
    ``.length``. A nested pair of loops over the same integer — ``i < GRID`` and
    ``j < GRID`` — is a grid, not an all-pairs walk, and is left alone.
    """
    found = []
    for outer_header, outer_start, outer_end in loops:
        outer_bound = _LENGTH_BOUND_RE.search(blanked, outer_header, outer_start)
        outer_var = _loop_variable(blanked, outer_header, outer_start)
        for header, body_start, _body_end in loops:
            if not (outer_start <= header < outer_end):
                continue
            pair = _PAIR_HEADER_RE.search(blanked, header, body_start)
            if pair is not None and outer_var and pair.group(2) == outer_var:
                found.append(header)
                continue
            inner_bound = _LENGTH_BOUND_RE.search(blanked, header, body_start)
            if (outer_bound is not None and inner_bound is not None
                    and outer_bound.group(1) == inner_bound.group(1)):
                found.append(header)
    return found


def _loop_variable(blanked: str, header: int, body_start: int) -> str | None:
    """The counter a ``for`` header declares, when it declares one."""
    match = re.search(r"(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=",
                      blanked[header:body_start])
    return match.group(1) if match else None


def scan_cost(source: str) -> list[Cost]:
    """Every per-frame cost this sketch pays that a frame cannot afford.

    Five rules, in the order they have actually cost this project time. All but
    the last two are restricted to ``draw()``, because the same call in
    ``setup()`` is paid once and is exactly where the repair should move it to.
    """
    blanked = _blank(source)
    starts = _line_starts(blanked)
    webgl = bool(_WEBGL_RE.search(blanked))
    bodies = _draw_bodies(blanked)
    found: list[Cost] = []
    seen: set[tuple[str, str, int]] = set()

    def add(rule: str, name: str, position: int, sentence: str) -> None:
        line = _line_of(starts, position)
        if (rule, name, line) in seen:
            return
        seen.add((rule, name, line))
        found.append(Cost(rule=rule, name=name, line=line, sentence=sentence))

    draw_loops = []
    for start, stop in bodies:
        draw_loops.extend(_loops_within(blanked, start, stop))
    loop_bodies = [(body_start, body_end) for _h, body_start, body_end in draw_loops]

    if webgl:
        for name in GEOMETRY_CALLS:
            for position in _call_positions(blanked, name, receiver=True):
                if not _inside(position, loop_bodies):
                    continue
                if not _runs_often(blanked, draw_loops, position):
                    continue
                add("geometry_in_loop", name, position,
                    f"`{name}()` is called inside a loop inside `draw()`, so this "
                    f"sketch builds one lit mesh per item per frame; draw the "
                    f"items as one `beginShape(POINTS)` or build the mesh once in "
                    f"`setup()` with `buildGeometry()`")

        for name in IMMEDIATE_CALLS:
            for position in _call_positions(blanked, name, receiver=True):
                if not _inside(position, loop_bodies):
                    continue
                if not _runs_often(blanked, draw_loops, position):
                    continue
                add("immediate_in_loop", name, position,
                    f"`{name}()` inside a loop inside `draw()` is immediate mode in "
                    f"WEBGL — every call builds and uploads its own vertex buffer; "
                    f"batch them into one "
                    f"`beginShape({'LINES' if name == 'line' else 'POINTS'})` with "
                    f"`vertex()` per point")

    for position in _all_pairs(blanked, draw_loops):
        add("all_pairs", "for", position,
            "this is an all-pairs loop inside `draw()`: every item is compared "
            "with every other one, so the work grows with the square of the count; "
            "use a spatial hash, or compare squared distances over a capped "
            "neighbour list")

    for name in ALLOCATING_CALLS:
        for position in _call_positions(blanked, name):
            if not _inside(position, bodies):
                continue
            add("allocation_in_draw", name, position,
                f"`{name}()` is called inside `draw()`, so this sketch allocates "
                f"it again on every frame; do it once in `setup()` and keep the "
                f"result in a variable")

    for position in _call_positions(blanked, FILTER_CALL):
        if not _inside(position, loop_bodies):
            continue
        add("filter_in_loop", FILTER_CALL, position,
            "`filter()` is a full-canvas pass and it is inside a loop, so the "
            "whole canvas is re-processed once per iteration; call it at most "
            "once per frame")

    found.sort(key=lambda cost: (cost.line, cost.rule, cost.name))
    return found


def scan_all(source: str) -> list:
    """Both scans over one sketch, in line order: the findings, all of them."""
    findings = list(scan(source)) + list(scan_cost(source))
    findings.sort(key=lambda finding: (finding.line, finding.sentence))
    return findings


def scan_dir(sketch_dir) -> list:
    """:func:`scan_all` over ``<sketch_dir>/sketch.js``; empty when there is none.

    An attempt that never got as far as a sketch file (a malformed executor
    response, a directory the gate refused) is not an error here: there is
    simply nothing to say about it.
    """
    path = Path(sketch_dir) / SKETCH_FILE
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return scan_all(source)


def evidence_lines(findings) -> list[str]:
    """The findings as the lines that go into the repair attempt's evidence.

    One shape for both scans, because the executor should not have to learn two:
    what is wrong, then where it is.
    """
    return [
        f"preflight: {finding.sentence} ({SKETCH_FILE} line {finding.line})"
        for finding in findings
    ]
