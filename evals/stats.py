"""Exact statistics for the eval instrument, and the power arithmetic behind it.

Three things live here, and they were previously scattered or missing:

1. **The exact tests.** `mcnemar_exact_p` (paired binary) came from
   `evals/compare.py`; `fisher_exact_greater` (unpaired) came from
   `scripts/ensemble_experiment.py`. Both are computed from `math.comb` in the
   standard library. scipy is not a dependency of this project and is not
   becoming one for four lines of arithmetic.

2. **Interval estimates.** Wilson for a proportion, and the exact
   Clopper-Pearson interval used for the published showcase bounds. A
   significance verdict on its own hides how wide the estimate is, and this
   project's headline number is a range for exactly that reason.

3. **Power.** This is the new part, and it is the reason the file exists.
   `ROADMAP.md` section 4 said "at n=28 the instrument can't see anything
   smaller than about six prompts". That figure is a *significance threshold at
   the observed churn* — how lopsided a split has to be before it clears alpha
   — not a *power* calculation, which asks how large a real difference has to
   be before this instrument would reliably notice it. The two differ by about
   a factor of two, and the second is the one that decides whether a corpus is
   big enough. See `docs/eval-methodology.md`.

**Reporting rule, enforced by `render_paired`/`render_unpaired` rather than
left to discipline: a p-value never travels alone.** Every rendering carries
the contingency table and n. A bare p-value is the shape of every measurement
mistake this project has already made and published.

For a paired binary design McNemar's power depends on the **discordant pair
rate** — the fraction of items that change outcome between two runs — not on
the total item count directly. An item that both arms pass, or both fail,
contributes nothing to the test. That is why banding the corpus (see
`evals/corpus.py`) is a power intervention and not bookkeeping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from math import comb
from typing import Sequence

__all__ = [
    "MEASURED_DISCORDANT_RATE",
    "PROJECTED_RATES",
    "PROJECTION_MARKER",
    "PairedResult",
    "PsiCell",
    "UnpairedResult",
    "clopper_pearson",
    "discordance",
    "fisher_exact_greater",
    "format_psi_cell",
    "mcnemar_exact_p",
    "mcnemar_power",
    "min_detectable",
    "min_detectable_effect",
    "paired_test",
    "power_curve",
    "psi_grid",
    "render_paired",
    "render_psi_grid",
    "render_unpaired",
    "required_n",
    "unpaired_test",
    "wilson",
]

# The one measured value. 18 of 28 items changed outcome between prompt set v3
# on Aug 8 and v3 on Aug 11 — the only pair of committed runs made under an
# identical configuration, and the noise floor `evals/README.md` publishes.
# Every other rate this module can be handed is a projection and is labelled as
# one; nothing here may report a different *measured* rate.
MEASURED_DISCORDANT_RATE = 18 / 28

# The hypothetical floors the psi grid explores, in `docs/eval-methodology.md`
# §7.1. 0.5 is not a round number: under independent runs an item discordant
# with probability 2*p*(1-p) can never exceed 0.5, so it is the ceiling any
# unpinned generator can produce and the measured 0.643 already sits above it.
PROJECTED_RATES = (0.5, 0.4, 0.32, 0.25)

# Printed on every cell computed at a rate that was not measured. A projection
# that reads like a measurement is the specific mistake this project has
# already published twice.
PROJECTION_MARKER = "*"


# -- exact tests -------------------------------------------------------------

def mcnemar_exact_p(b: int, c: int, one_sided: bool = False) -> float:
    """Exact McNemar on discordant counts: b regressions, c improvements.

    Under the null the b+c items that changed are coin flips, so this is a
    sign test. Exact rather than chi-square because these counts are small —
    chi-square is not trustworthy below about 25 discordant pairs, and this
    eval set has never produced that many.

    One-sided is the right test for a promote/delete decision: a candidate is
    only ever promoted on evidence of improvement, and a regression and a wash
    lead to the same action. v1 -> v3, this project's best change, is p = 0.033
    one-sided and p = 0.065 two-sided; a two-sided gate would have rejected it.
    Use two-sided when the question is "did these differ at all", which is what
    a *negative control* asks.
    """
    if b < 0 or c < 0:
        raise ValueError("discordant counts cannot be negative")
    n = b + c
    if n == 0:
        return 1.0
    if one_sided:
        return min(1.0, _over_two_pow(sum(comb(n, i) for i in range(c, n + 1)), n))
    k = min(b, c)
    return min(1.0, 2 * _over_two_pow(sum(comb(n, i) for i in range(k + 1)), n))


def _over_two_pow(numerator: int, exponent: int) -> float:
    """numerator / 2**exponent, without overflowing float on large exponents.

    The power calculations reach n in the hundreds, where both the binomial
    tail and 2**n are far outside float range even though their ratio is a
    perfectly ordinary probability. Truncating the numerator to its top 64 bits
    and scaling with `ldexp` keeps the ratio to about 19 significant digits,
    which is more than double precision can hold anyway.
    """
    if numerator == 0:
        return 0.0
    shift = max(0, numerator.bit_length() - 64)
    return math.ldexp(numerator >> shift, shift - exponent)


def fisher_exact_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided p for a 2x2 table: is [a of a+b] better than [c of c+d]?

    Exact hypergeometric tail. The right test when the two arms are *not*
    paired — different artifacts produced by different architectures, with no
    correspondence between run i on one side and run i on the other. Using a
    paired test there means inventing a pairing that does not exist.
    """
    for value in (a, b, c, d):
        if value < 0:
            raise ValueError("cell counts cannot be negative")
    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, col1 = a + b, a + c
    total = comb(n, col1)
    if total == 0:
        return 1.0
    p = 0.0
    for k in range(a, min(row1, col1) + 1):
        p += comb(row1, k) * comb(n - row1, col1 - k)
    return p / total


# -- interval estimates ------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — behaves at 0/n and n/n, unlike the normal one."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval, by inverting the binomial tail.

    No scipy, so the beta quantiles are found by bisection on the exact tail
    sums rather than from a closed form. Exact to about 1e-12, which is far
    beyond what any of these sample sizes justify reporting.
    """
    if n == 0:
        return (0.0, 1.0)
    if not 0 <= k <= n:
        raise ValueError("k must be between 0 and n")

    def upper_tail(p: float, at_least: int) -> float:
        return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(at_least, n + 1))

    def lower_tail(p: float, at_most: int) -> float:
        return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(0, at_most + 1))

    lo, hi = 0.0, 1.0
    if k > 0:
        a, b = 0.0, 1.0
        for _ in range(200):
            mid = (a + b) / 2
            if upper_tail(mid, k) < alpha / 2:
                a = mid
            else:
                b = mid
        lo = a
    if k < n:
        a, b = 0.0, 1.0
        for _ in range(200):
            mid = (a + b) / 2
            if lower_tail(mid, k) < alpha / 2:
                b = mid
            else:
                a = mid
        hi = b
    return (lo, hi)


# -- power -------------------------------------------------------------------

@lru_cache(maxsize=None)
def min_detectable(n_discordant: int, alpha: float = 0.05) -> int | None:
    """How many of n discordant pairs must fall one way to clear alpha.

    This is a **significance threshold**, not a power statement, and conflating
    the two is what produced the "about six prompts" figure in ROADMAP section
    4. It answers "if I observe this much churn, how lopsided must it be",
    which is only useful after the run. `min_detectable_effect` answers the
    question that decides corpus size, and gives a roughly twice larger number.

    Returns None when no split of n_discordant pairs can clear alpha — the
    normal answer for a category of 4-6 prompts.
    """
    for k in range(n_discordant + 1):
        p = sum(comb(n_discordant, i) for i in range(k, n_discordant + 1)) / (2 ** n_discordant)
        if p <= alpha:
            return k
    return None


def _binom_pmf(k: int, n: int, p: float) -> float:
    """Binomial pmf in log space — `comb(n, k)` alone overflows float past n=1030."""
    if k < 0 or k > n:
        return 0.0
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    log_p = (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )
    return math.exp(log_p) if log_p > -745 else 0.0


@lru_cache(maxsize=None)
def _critical_c(d: int, alpha: float, one_sided: bool) -> int | None:
    """Smallest improvement count among d discordant pairs that clears alpha.

    Accumulated from the top of the tail downwards rather than by re-summing
    `mcnemar_exact_p` for every candidate c. That is the same answer computed
    in O(d) instead of O(d^2) big-integer terms, which is the difference
    between a power curve that returns and one that does not.
    """
    if d == 0:
        return None
    tail = 0
    limit = alpha if one_sided else alpha / 2
    for c in range(d, -1, -1):
        tail += comb(d, c)
        if _over_two_pow(tail, d) > limit:
            return c + 1 if c + 1 <= d else None
    return 0


def mcnemar_power(
    n: int,
    discordant_rate: float,
    delta: float,
    alpha: float = 0.05,
    one_sided: bool = True,
) -> float:
    """Exact power of McNemar's test over n paired items.

    The model, stated because a power number is worthless without one:

      * each item is discordant with probability `discordant_rate` (psi),
        estimated from two runs of an identical configuration;
      * a discordant item goes to the candidate arm with probability
        pi = (delta/psi + 1) / 2, so the marginal difference between the arms
        is delta = psi * (2*pi - 1);
      * the number discordant D ~ Binomial(n, psi), and conditional on D the
        improvements C ~ Binomial(D, pi).

    Power is the probability that the exact test on (D-C, C) clears alpha,
    summed over D. Everything is computed exactly; nothing is simulated.

    Raises ValueError when `delta` exceeds `discordant_rate`. That is not a
    numerical edge case but a real constraint: a paired design cannot show a
    marginal difference larger than the fraction of items that move at all.
    """
    if not 0.0 <= discordant_rate <= 1.0:
        raise ValueError("discordant_rate must be a probability")
    if delta < 0:
        raise ValueError("delta must be non-negative")
    if n < 0:
        raise ValueError("n must be non-negative")
    if discordant_rate == 0.0:
        return 0.0
    if delta > discordant_rate:
        raise ValueError(
            f"delta={delta} exceeds the discordant rate {discordant_rate}: a paired "
            "test cannot resolve a marginal difference larger than the fraction of "
            "items that change outcome at all"
        )
    pi = (delta / discordant_rate + 1) / 2
    # Only the plausible range of D contributes. Ten standard deviations either
    # side of the mean leaves out less than 1e-20 of the mass and keeps the
    # calculation linear rather than quadratic in n.
    spread = 10 * math.sqrt(n * discordant_rate * (1 - discordant_rate)) + 10
    lo_d = max(0, int(n * discordant_rate - spread))
    hi_d = min(n, int(n * discordant_rate + spread) + 1)
    total = 0.0
    for d in range(lo_d, hi_d + 1):
        p_d = _binom_pmf(d, n, discordant_rate)
        if p_d < 1e-15:
            continue
        threshold = _critical_c(d, alpha, one_sided)
        if threshold is None:
            continue
        total += p_d * sum(_binom_pmf(c, d, pi) for c in range(threshold, d + 1))
    return total


def min_detectable_effect(
    n: int,
    discordant_rate: float,
    alpha: float = 0.05,
    power: float = 0.80,
    one_sided: bool = True,
) -> float | None:
    """The smallest true difference this many items can detect at `power`.

    Returned as a proportion of the corpus (0.15 means "fifteen percentage
    points"). Multiply by n for "how many prompts". None when no effect the
    design can express reaches the requested power — which happens whenever
    even delta == discordant_rate falls short.
    """
    ceiling = mcnemar_power(n, discordant_rate, discordant_rate, alpha, one_sided)
    if ceiling < power:
        return None
    lo, hi = 0.0, discordant_rate
    for _ in range(60):
        mid = (lo + hi) / 2
        if mcnemar_power(n, discordant_rate, mid, alpha, one_sided) < power:
            lo = mid
        else:
            hi = mid
    return hi


def required_n(
    delta: float,
    discordant_rate: float,
    alpha: float = 0.05,
    power: float = 0.80,
    cap: int = 1000,
    one_sided: bool = True,
) -> int | None:
    """Smallest corpus size that detects `delta` at `power`. None above `cap`.

    None is a real answer and usually the interesting one. Report it as "this
    is not resolvable at a size anyone will run", not as a missing number.
    """
    if delta > discordant_rate:
        return None
    if mcnemar_power(cap, discordant_rate, delta, alpha, one_sided) < power:
        return None
    lo, hi = 1, cap
    while lo < hi:
        mid = (lo + hi) // 2
        if mcnemar_power(mid, discordant_rate, delta, alpha, one_sided) >= power:
            hi = mid
        else:
            lo = mid + 1
    return lo


def power_curve(
    sizes: list[int],
    discordant_rate: float,
    delta: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> list[dict]:
    """One row per corpus size: detectable effect, and power at a target delta.

    The curve is the deliverable, not a single number — it is what makes the
    cost of each additional prompt visible, and it is what says plainly when
    the required corpus is one nobody will ever run.
    """
    rows = []
    for n in sizes:
        mde = min_detectable_effect(n, discordant_rate, alpha, power)
        rows.append(
            {
                "n": n,
                "mde": mde,
                "mde_items": None if mde is None else mde * n,
                "power_at_delta": mcnemar_power(n, discordant_rate, delta, alpha),
            }
        )
    return rows


def discordance(outcomes_a: dict[str, bool], outcomes_b: dict[str, bool]) -> dict:
    """Observed discordance between two runs over the items they share.

    The estimate every power number downstream depends on, so it reports its
    own n and an interval rather than a bare fraction.
    """
    shared = sorted(set(outcomes_a) & set(outcomes_b))
    if not shared:
        raise ValueError("runs share no item ids — nothing to estimate from")
    up = [i for i in shared if not outcomes_a[i] and outcomes_b[i]]
    down = [i for i in shared if outcomes_a[i] and not outcomes_b[i]]
    k = len(up) + len(down)
    lo, hi = wilson(k, len(shared))
    return {
        "n": len(shared),
        "discordant": k,
        "up": len(up),
        "down": len(down),
        "rate": k / len(shared),
        "ci95": (lo, hi),
    }


# -- results that carry their own evidence -----------------------------------

@dataclass(frozen=True)
class PairedResult:
    """A McNemar result with the table that produced it attached.

    The table is not decoration. `both_pass` and `both_fail` are what say
    whether the corpus had any resolving power at all: a comparison where every
    item lands in those two cells produced no evidence, however small the
    p-value on the two items that moved.
    """

    n: int
    both_pass: int
    a_only: int
    b_only: int
    both_fail: int
    p_one_sided: float
    p_two_sided: float
    alpha: float = 0.05

    @property
    def discordant(self) -> int:
        return self.a_only + self.b_only

    @property
    def net(self) -> int:
        return self.b_only - self.a_only

    @property
    def significant_one_sided(self) -> bool:
        return self.p_one_sided <= self.alpha

    @property
    def significant_two_sided(self) -> bool:
        return self.p_two_sided <= self.alpha


@dataclass(frozen=True)
class UnpairedResult:
    a_pass: int
    a_total: int
    b_pass: int
    b_total: int
    p_one_sided: float
    alpha: float = 0.05

    @property
    def significant(self) -> bool:
        return self.p_one_sided <= self.alpha


def paired_test(
    outcomes_a: dict[str, bool], outcomes_b: dict[str, bool], alpha: float = 0.05
) -> PairedResult:
    shared = sorted(set(outcomes_a) & set(outcomes_b))
    if not shared:
        raise ValueError("runs share no item ids — nothing to test")
    both_pass = sum(1 for i in shared if outcomes_a[i] and outcomes_b[i])
    both_fail = sum(1 for i in shared if not outcomes_a[i] and not outcomes_b[i])
    a_only = sum(1 for i in shared if outcomes_a[i] and not outcomes_b[i])
    b_only = sum(1 for i in shared if not outcomes_a[i] and outcomes_b[i])
    return PairedResult(
        n=len(shared),
        both_pass=both_pass,
        a_only=a_only,
        b_only=b_only,
        both_fail=both_fail,
        p_one_sided=mcnemar_exact_p(a_only, b_only, one_sided=True),
        p_two_sided=mcnemar_exact_p(a_only, b_only),
        alpha=alpha,
    )


def unpaired_test(
    a_pass: int, a_total: int, b_pass: int, b_total: int, alpha: float = 0.05
) -> UnpairedResult:
    """Fisher exact, one-sided, asking whether arm B beats arm A."""
    if a_total <= 0 or b_total <= 0:
        raise ValueError("both arms need at least one trial")
    return UnpairedResult(
        a_pass=a_pass,
        a_total=a_total,
        b_pass=b_pass,
        b_total=b_total,
        p_one_sided=fisher_exact_greater(
            b_pass, b_total - b_pass, a_pass, a_total - a_pass
        ),
        alpha=alpha,
    )


def render_paired(result: PairedResult, label_a: str = "A", label_b: str = "B") -> str:
    """The only sanctioned way to print a paired result.

    Table, counts, interval, then p — in that order, and never p alone.
    """
    lo, hi = wilson(result.discordant, result.n)
    need = min_detectable(result.discordant, result.alpha)
    lines = [
        f"n = {result.n} paired items   ({label_a} vs {label_b})",
        "",
        f"                    {label_b} pass  {label_b} fail",
        f"    {label_a} pass  {result.both_pass:>10}  {result.a_only:>9}",
        f"    {label_a} fail  {result.b_only:>10}  {result.both_fail:>9}",
        "",
        f"  discordant : {result.discordant} of {result.n} "
        f"({result.discordant / result.n:.0%}, 95% CI {lo:.0%}-{hi:.0%})",
        f"  improved   : {result.b_only}      regressed: {result.a_only}"
        f"      net: {result.net:+d}",
        f"  McNemar exact  one-sided p = {result.p_one_sided:.4f}"
        f"   two-sided p = {result.p_two_sided:.4f}",
    ]
    if need is None:
        lines.append(
            f"  power      : {result.discordant} discordant pairs cannot reach "
            f"alpha={result.alpha} in any split"
        )
    else:
        lines.append(
            f"  power      : {need} of {result.discordant} discordant pairs had to go "
            f"one way to clear alpha={result.alpha}; got {result.b_only}"
        )
    return "\n".join(lines)


def render_unpaired(result: UnpairedResult, label_a: str = "A", label_b: str = "B") -> str:
    a_lo, a_hi = wilson(result.a_pass, result.a_total)
    b_lo, b_hi = wilson(result.b_pass, result.b_total)
    return "\n".join(
        [
            "                     pass     fail",
            f"    {label_a:<12} {result.a_pass:>7} {result.a_total - result.a_pass:>8}",
            f"    {label_b:<12} {result.b_pass:>7} {result.b_total - result.b_pass:>8}",
            "",
            f"  {label_a}: {result.a_pass}/{result.a_total} "
            f"({result.a_pass / result.a_total:.0%}, 95% CI {a_lo:.0%}-{a_hi:.0%})",
            f"  {label_b}: {result.b_pass}/{result.b_total} "
            f"({result.b_pass / result.b_total:.0%}, 95% CI {b_lo:.0%}-{b_hi:.0%})",
            f"  Fisher exact one-sided p = {result.p_one_sided:.4f}",
        ]
    )


# -- the noise floor as a parameter ------------------------------------------
#
# Everything above takes psi as an argument already. What was missing is the
# two-dimensional view: the published curve answers "what can n items see at
# the measured floor", and the question this project actually faces is "what
# would a lower floor be worth". The published model is delta proportional to
# sqrt(psi / n), so halving psi buys exactly what doubling n buys — and pinning
# the generator's temperature and seed is a plausible, removable and unmeasured
# contributor to psi. `docs/experiments/noise-floor-under-pinned-sampling.md`
# is the design that measures it; this is the arithmetic that turns the answer
# into a corpus size without anyone re-deriving it.


@dataclass(frozen=True)
class PsiCell:
    """One (n, psi) cell of the grid, carrying whether it was measured.

    `projected` is not presentation metadata. A cell computed at a discordant
    rate nobody has observed is a hypothesis about an instrument, and this
    project has twice published a number that turned out to have been produced
    under conditions its reader assumed were the measured ones.
    """

    n: int
    discordant_rate: float
    projected: bool
    mde: float | None
    mde_items: float | None
    power_at_delta: float
    target_reachable: bool
    required_n: int | None


def psi_grid(
    sizes: Sequence[int],
    rates: Sequence[float],
    delta: float,
    measured_rate: float = MEASURED_DISCORDANT_RATE,
    alpha: float = 0.05,
    power: float = 0.80,
) -> list[PsiCell]:
    """Detectable effect and power at `delta`, over corpus size crossed with psi.

    A cell is `target_reachable` when the corpus detects `delta` at `power` —
    equivalently when the minimum detectable effect has fallen to `delta` or
    below. That is the line the pre-registered 15-point target has to cross,
    and at the measured floor it never does at any size this project will run.
    """
    if not sizes:
        raise ValueError("psi_grid needs at least one corpus size")
    if not rates:
        raise ValueError("psi_grid needs at least one discordant rate")
    cells: list[PsiCell] = []
    for rate in rates:
        projected = not math.isclose(rate, measured_rate, rel_tol=1e-9, abs_tol=1e-9)
        needed = required_n(delta, rate, alpha, power) if delta <= rate else None
        for n in sizes:
            mde = min_detectable_effect(n, rate, alpha, power)
            cells.append(
                PsiCell(
                    n=n,
                    discordant_rate=rate,
                    projected=projected,
                    mde=mde,
                    mde_items=None if mde is None else mde * n,
                    power_at_delta=(
                        mcnemar_power(n, rate, delta, alpha) if delta <= rate else float("nan")
                    ),
                    target_reachable=mde is not None and mde <= delta + 1e-12,
                    required_n=needed,
                )
            )
    return cells


def format_psi_cell(text: str, projected: bool, *, marked: bool = True) -> str:
    """Render one cell, refusing to print a projection without its marker.

    The refusal is the point. Marking projections is the kind of discipline
    that survives exactly as long as nobody is in a hurry, so it is enforced
    here rather than left to whoever writes the next renderer.
    """
    if projected and not marked:
        raise ValueError(
            "a cell computed at a projected discordant rate cannot be printed "
            "without its projection marker: only psi = 18/28 has been measured, "
            "and an unmarked cell reads as an observation"
        )
    return f"{text}{PROJECTION_MARKER}" if projected else text


def render_psi_grid(
    cells: Sequence[PsiCell],
    delta: float,
    measured_rate: float = MEASURED_DISCORDANT_RATE,
    per_item_minutes: float | None = None,
    corpus_n: int | None = None,
    power: float = 0.80,
) -> str:
    """The grid, with every projected cell marked and the legend attached.

    Reading order: the measured column first, then what a lower floor would
    buy. `corpus_n` marks the row the corpus this project actually has lands
    on, so "what would pinning be worth" has a visible answer rather than an
    inferred one.
    """
    if not cells:
        raise ValueError("nothing to render — psi_grid returned no cells")
    rates: list[float] = []
    for cell in cells:
        if cell.discordant_rate not in rates:
            rates.append(cell.discordant_rate)
    sizes: list[int] = []
    for cell in cells:
        if cell.n not in sizes:
            sizes.append(cell.n)
    index = {(c.n, c.discordant_rate): c for c in cells}

    width = 15
    header = f"{'n':>5}  {'comparison':>11}  " + "  ".join(
        format_psi_cell(
            f"psi={rate:.3f}",
            not math.isclose(rate, measured_rate, rel_tol=1e-9, abs_tol=1e-9),
        ).rjust(width)
        for rate in rates
    )
    lines = [
        f"Minimum detectable effect at {power:.0%} power / power at "
        f"{delta:.0%}, by corpus size and noise floor",
        "",
        header,
        "-" * len(header),
    ]
    for n in sizes:
        cost = "-" if per_item_minutes is None else f"{2 * n * per_item_minutes / 60:.0f} h"
        row = f"{n:>5}  {cost:>11}  "
        parts = []
        for rate in rates:
            cell = index[(n, rate)]
            mde = "not reachable" if cell.mde is None else f"{cell.mde:.1%}"
            power_text = (
                "  n/a" if cell.power_at_delta != cell.power_at_delta
                else f"{cell.power_at_delta:5.2f}"
            )
            text = f"{mde:>6}/{power_text}"
            if cell.target_reachable:
                text = "[" + text.strip() + "]"
            parts.append(format_psi_cell(text.rjust(width - 1), cell.projected))
        marker = "  <- the corpus this project has" if corpus_n == n else ""
        lines.append(row + "  ".join(p.rjust(width) for p in parts) + marker)

    lines.append("")
    lines.append(
        f"  each cell: minimum detectable effect / power at {delta:.0%}."
        f"  [brackets] = {delta:.0%} is reachable at {power:.0%} power."
    )
    lines.append(
        f"  {PROJECTION_MARKER} PROJECTION. Only psi = {measured_rate:.3f} (18 of 28, "
        "prompt set v3 against itself) has been measured."
    )
    lines.append(
        "    Every starred column is what the instrument WOULD see at a floor "
        "nobody has observed."
    )
    for rate in rates:
        cell = index[(sizes[0], rate)]
        needed = "not reachable at any size considered" if cell.required_n is None else (
            f"n = {cell.required_n}"
        )
        label = format_psi_cell(f"psi={rate:.3f}", cell.projected)
        lines.append(
            f"    {label:>12}: detecting {delta:.0%} at {power:.0%} power needs {needed}"
        )
    return "\n".join(lines)
