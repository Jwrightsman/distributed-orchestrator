"""Showcase artifacts — the "it opens in your browser" moment, and how to judge it.

One registry, imported by both `cli.py` (to run one) and
`scripts/showcase_reliability.py` (to measure one), so the thing measured is
byte-identical to the thing demoed. Two copies of a pitch string would let the
measured number drift away from what the video actually runs.

WHY THERE IS MORE THAN ONE
--------------------------
`snake` is the original showcase and it measures **2/10 playable**
(docs/showcase-ceiling.md). The failures are semantically dead but
syntactically fine: no JS errors, nothing ever drawn. The ceiling doc's own
conclusion was that a one-shot interactive game is past what a 4B model does
reliably, and that the remaining leverage is a *less coupled* artifact — same
browser moment, far less integration risk.

The other entries here are that experiment. Snake stays exactly as it was: it
is the honest hard case, and it is still what `--demo-showcase` runs by default.

HOW THE BAR IS SET
------------------
`checks` differs per artifact, on purpose. "Would this embarrass us on camera"
means something different for a game than for a chart — a game that does not
move is broken, a chart that does not move is a chart. Each artifact carries
roughly four criteria of comparable strictness, and each one is a failure that
has actually been observed in generated output rather than a hypothetical.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    """A showcase pitch plus the checks that decide whether it came out right."""

    id: str
    title: str
    blurb: str
    pitch: str

    # Structural checks, all verified in a real headless browser.
    #
    # needs_canvas also decides how "did it draw anything" is measured, because
    # the two artifact kinds are not comparable: canvas ink is lit pixels, DOM
    # ink is visibly-sized elements. It matters for the text checks too — text
    # painted on a canvas is invisible to innerText, so a canvas artifact
    # cannot be checked for its labels or for a visible NaN. Anything whose
    # correctness is checkable through text must be DOM-rendered.
    needs_canvas: bool = True
    # Minimum ink. For canvas: lit pixels (50 catches "blank", higher catches
    # "drew one dot"). For DOM: elements with a bounding box over 100px².
    min_ink: int = 50
    # Must the picture change with no input? True for anything whose whole
    # point is motion; False for a still artifact like a chart.
    needs_animation: bool = True
    # Press ArrowRight and require the picture to change. Only meaningful for
    # something the arrow keys actually steer.
    needs_key_response: bool = False
    # Visible text that means it came out wrong. Matched case-insensitively
    # against rendered text, not source — `NaN` in a comment is fine.
    forbidden_text: tuple[str, ...] = ()
    # Substrings that must ALL appear in the rendered text (data labels, etc).
    required_text: tuple[str, ...] = ()
    # Extra note printed with the result, for reading a log months later.
    notes: str = ""

    scoring: list[str] = field(default_factory=list)


# The dataset the chart candidate must render. Hardcoded here so the pitch, the
# checker's required labels, and the demo all agree on it.
CHART_DATA = [
    ("Mon", 12), ("Tue", 19), ("Wed", 7), ("Thu", 23),
    ("Fri", 31), ("Sat", 15), ("Sun", 9),
]
_CHART_JS = "[" + ", ".join(f'{{label: "{k}", value: {v}}}' for k, v in CHART_DATA) + "]"


# Shared preamble. Every one of these clauses exists because its absence
# produced a measured failure at some point: split files, missing doctype,
# frameworks that need installing, and start screens that look broken on load.
_ONE_FILE = (
    "The final deliverable must be one complete HTML document starting with "
    "<!DOCTYPE html> that runs by double-clicking the file — no external files, "
    "no frameworks, no libraries, no image assets, and no network requests. "
)


SNAKE = Candidate(
    id="snake",
    title="Neon Snake game",
    blurb="the hard case — a playable game in one file",
    # Unchanged from the string this was measured at 2/10 with. Do not edit
    # without re-measuring; docs/showcase-ceiling.md cites this exact wording.
    pitch=(
        "Build a retro Snake game as ONE single self-contained HTML file with all CSS and "
        "JavaScript inline in that file. Dark background with neon-glow styling, a live score "
        "display, arrow-key controls, collision detection, and a game-over screen with a "
        "restart button. The final deliverable must be one complete HTML document starting "
        "with <!DOCTYPE html> that runs by double-clicking the file — no external files, "
        "no frameworks, no image assets (draw everything on a <canvas>). "
        "REQUIRED BEHAVIOUR ON LOAD: the snake must already be moving the moment the page "
        "opens — no start screen, no title screen, no 'press any key' prompt, and no click "
        "required to begin. There must be exactly ONE overlay element, used only for game "
        "over; it starts with style=\"display:none\" in the HTML, is shown only when the "
        "snake dies, and is hidden again by the restart button. The text 'GAME OVER' must "
        "never be visible before the snake has died."
    ),
    needs_animation=True,
    needs_key_response=True,
    forbidden_text=("game over",),
    notes="Measured 2/10 (docs/showcase-ceiling.md). Kept as the honest hard case.",
)


CLOCK = Candidate(
    id="clock",
    title="Neon analog clock",
    blurb="tells the real time, sweeps every second",
    pitch=(
        "Build an analog clock as ONE single self-contained HTML file with all CSS and "
        "JavaScript inline in that file. Draw a round clock face on a <canvas>: an outer "
        "rim, twelve hour markers, an hour hand, a minute hand, and a thinner second hand, "
        "on a dark background with neon-glow styling. Below the clock face, show the same "
        "time as digital text in HH:MM:SS inside an HTML element "
        "<div id=\"digital\"></div>, updated with textContent on every redraw — that div is "
        "real HTML, not text drawn on the canvas. "
        + _ONE_FILE +
        "REQUIRED BEHAVIOUR ON LOAD: the clock must already be drawn and running the moment "
        "the page opens — no start button, no click required, nothing to press. Call "
        "requestAnimationFrame (or setInterval at 100ms or faster) from the top level of "
        "your script so redrawing begins immediately, and read the current time with a fresh "
        "'new Date()' inside every redraw so the hands track the computer's real clock and "
        "the second hand visibly moves. Compute each hand's angle from that Date object — "
        "do not animate from a fixed starting angle. "
        "The page must never display the text 'NaN' or 'undefined': if you compute an angle "
        "or a padded number, check it is a real number before drawing it."
    ),
    needs_animation=True,
    forbidden_text=("nan", "undefined"),
    required_text=(":",),  # the HH:MM:SS readout, proof the div is real HTML
    notes="Self-evidently correct on camera — the hands match the wall clock.",
)


CHART = Candidate(
    id="chart",
    title="Neon bar chart",
    blurb="renders a real dataset, labelled and readable",
    pitch=(
        "Build a bar chart as ONE single self-contained HTML file with all CSS and "
        "JavaScript inline in that file. Chart exactly this data, hardcoded near the top of "
        f"your script as a JavaScript array: const data = {_CHART_JS}; "
        "Build the chart out of HTML elements styled with CSS — one <div> per bar inside a "
        "flex row, each bar's height set as a percentage of the tallest value (31) so the "
        "tallest bar nearly fills the plot area. Do NOT use <canvas> and do not draw with "
        "JavaScript graphics calls. Under each bar put its day label as text, above each bar "
        "put its numeric value as text, add a heading reading 'Tasks Completed', and style it "
        "with a dark background and neon-glow bars. "
        + _ONE_FILE +
        "REQUIRED BEHAVIOUR ON LOAD: the finished chart must be fully built the moment the "
        "page opens — no start button, no click required, no loading state. Generate the bars "
        "by looping over the data array and appending elements to the document, and call that "
        "function from the top level of your script after the container element exists. "
        "Every one of the seven day labels (Mon, Tue, Wed, Thu, Fri, Sat, Sun) and every one "
        "of the seven values must be visible as text on the page. "
        "The page must never display the text 'NaN' or 'undefined': compute each height from "
        "the data array and check it is a real number before setting it."
    ),
    # A chart is a still image by nature, so requiring self-motion would fail a
    # correct artifact. The animation criterion is replaced by a stricter
    # content one: every label and value must actually be on the page — which
    # is only checkable because this artifact is DOM-rendered, not canvas.
    needs_canvas=False,
    needs_animation=False,
    min_ink=8,  # 7 bars + a container, at minimum
    forbidden_text=("nan", "undefined"),
    required_text=tuple(k for k, _ in CHART_DATA) + tuple(str(v) for _, v in CHART_DATA),
    notes="Only candidate whose correctness is externally checkable — the data is known.",
)


PARTICLES = Candidate(
    id="particles",
    title="Neon particle field",
    blurb="pure motion — the lowest-risk visual",
    pitch=(
        "Build an animated particle field as ONE single self-contained HTML file with all "
        "CSS and JavaScript inline in that file. On a full-window <canvas> with a dark "
        "background, draw about 80 small glowing dots that drift in straight lines at "
        "different speeds, bounce off the edges of the canvas, and draw a faint connecting "
        "line between any two dots that are closer than 120 pixels apart. Neon colours, "
        "soft glow, slight motion trails. "
        + _ONE_FILE +
        "REQUIRED BEHAVIOUR ON LOAD: the animation must already be running the moment the "
        "page opens — no start button, no click required, nothing to press. Create the "
        "particle array and call requestAnimationFrame from the top level of your script so "
        "motion begins immediately, and update every particle's position on every frame so "
        "the picture is visibly different from one second to the next."
    ),
    # No correctness criterion at all — any code that draws and loops looks
    # right. That is the entire hypothesis being tested.
    needs_animation=True,
    min_ink=200,
    notes="No correct answer to get wrong. Expected most reliable, least informative.",
)


CANDIDATES: dict[str, Candidate] = {c.id: c for c in (SNAKE, CLOCK, CHART, PARTICLES)}

DEFAULT_CANDIDATE = "snake"


def get(candidate_id: str) -> Candidate:
    if candidate_id not in CANDIDATES:
        known = ", ".join(sorted(CANDIDATES))
        raise KeyError(f"Unknown showcase candidate {candidate_id!r}. Available: {known}")
    return CANDIDATES[candidate_id]


def describe() -> str:
    """One line per candidate, for --help text."""
    return "\n".join(f"  {c.id:<10} {c.title} — {c.blurb}" for c in CANDIDATES.values())
