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
import random
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
    "WilcoxonResult",
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
    "render_wilcoxon",
    "replicate_power",
    "required_n",
    "unpaired_test",
    "wilcoxon_signed_rank",
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
                    # A delta above psi is not a small effect, it is one a
                    # paired design cannot express at all — `mcnemar_power`
                    # raises on it. The minimum detectable effect is capped at
                    # psi, so without this guard a floor of 0.10 would report
                    # a 15-point target as reachable.
                    target_reachable=(
                        delta <= rate and mde is not None and mde <= delta + 1e-12
                    ),
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
        label = format_psi_cell(f"psi={rate:.3f}", cell.projected)
        if cell.required_n is None:
            reason = (
                "is larger than the floor itself, so no paired design can express it"
                if delta > rate
                else "is not reachable at any corpus size this considers"
            )
            lines.append(f"    {label:>12}: {delta:.0%} {reason}")
        else:
            lines.append(
                f"    {label:>12}: detecting {delta:.0%} at {power:.0%} power needs "
                f"n = {cell.required_n}"
            )
    return "\n".join(lines)


# -- a continuous endpoint for replicated items ------------------------------
#
# The alternative to more items is more runs per item, scoring each item by the
# fraction of its k runs that passed rather than by one Bernoulli draw. That is
# a continuous paired endpoint and it needs a different test.
#
# **Wilcoxon signed-rank rather than a paired t**, argued from the distribution
# rather than from habit: with k runs an item's rate lives on {0, 1/k, ..., 1},
# so at k=5 there are six possible values, the corpus is banded to put items at
# the floor and the ceiling deliberately, and a large share of paired
# differences are exactly zero. That distribution is discrete, bounded, heavily
# tied and visibly non-normal, and a t-test on 36 such differences is leaning
# on a central limit theorem that the shape does not earn. Wilcoxon assumes
# only symmetry of the differences under the null, which the paired design
# supplies by construction.
#
# The null distribution is computed exactly, by counting subsets of the
# observed ranks, so ties and small n are handled rather than approximated.
# scipy is not a dependency and is not becoming one.


@dataclass(frozen=True)
class WilcoxonResult:
    """A signed-rank result carrying the counts that produced it.

    Same rule as `PairedResult`: `zeros` and `n_nonzero` say whether the
    comparison had anything to work with. A test over 36 items where 34
    differences were exactly zero produced no evidence, whatever the p-value on
    the two that moved.
    """

    n: int
    n_nonzero: int
    zeros: int
    w_plus: float
    w_minus: float
    p_one_sided: float
    p_two_sided: float
    exact: bool
    alpha: float = 0.05

    @property
    def significant_one_sided(self) -> bool:
        return self.p_one_sided <= self.alpha


# Above this many non-zero differences the exact subset count is replaced by the
# tie-corrected normal approximation, unless a caller asks for exact anyway.
# The exact null is a subset-sum count costing O(m^3) big-integer additions,
# cheap at the sizes a study here produces and expensive at the sizes a power
# simulation reaches.
#
# **The approximation does not converge, and that is worth knowing rather than
# assuming.** `tests/test_eval_stats.py::test_signed_rank_approximation_agrees_
# with_exact` measures the largest disagreement over randomly drawn heavily-tied
# difference vectors: about 0.018 at m = 20, 0.009 at m = 30, and 0.008 at
# m = 60 and m = 100 — it plateaus. The cause is the lattice: with few distinct
# magnitudes the statistic moves in steps much larger than the half-unit
# continuity correction assumes.
#
# So: a study on the 36-item confirmatory set produces about 30 non-zero
# differences and is computed exactly. A larger one reports `exact = False`,
# `render_wilcoxon` says the p-value carries about +/-0.01, and a result within
# that of alpha should be recomputed with `exact=True` rather than believed.
MAX_EXACT_SIGNED_RANK = 60


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks of `values`, averaging over ties. 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = shared
        position = end + 1
    return ranks


@lru_cache(maxsize=4096)
def _signed_rank_counts(doubled_ranks: tuple[int, ...]) -> tuple[int, ...]:
    """Subset-sum counts over the doubled ranks — the exact null distribution.

    Under the null each difference is equally likely to carry a plus or a
    minus, so W+ is the sum of a uniformly random subset of the observed ranks.
    Ranks are doubled so averaged ties stay integers and the count is exact.
    """
    total = sum(doubled_ranks)
    counts = [0] * (total + 1)
    counts[0] = 1
    for rank in doubled_ranks:
        for value in range(total, rank - 1, -1):
            if counts[value - rank]:
                counts[value] += counts[value - rank]
    return tuple(counts)


def wilcoxon_signed_rank(
    differences: Sequence[float], alpha: float = 0.05, exact: bool | None = None
) -> WilcoxonResult:
    """Exact one- and two-sided Wilcoxon signed-rank on paired differences.

    Zero differences are dropped, Wilcoxon's original treatment, and counted in
    the result so the reader can see how much of the corpus said nothing. The
    one-sided hypothesis is that the differences are positive — the same
    directional question `mcnemar_exact_p` asks, for the same reason: a
    candidate is only ever promoted on evidence of improvement.

    `exact=None` chooses by `MAX_EXACT_SIGNED_RANK`; True forces the exact null
    whatever it costs, False forces the approximation. The result always says
    which one ran, so a p-value is never quietly approximate.
    """
    values = [float(d) for d in differences]
    if not values:
        raise ValueError("no paired differences — nothing to test")
    nonzero = [d for d in values if d != 0.0]
    zeros = len(values) - len(nonzero)
    if not nonzero:
        return WilcoxonResult(
            n=len(values),
            n_nonzero=0,
            zeros=zeros,
            w_plus=0.0,
            w_minus=0.0,
            p_one_sided=1.0,
            p_two_sided=1.0,
            exact=True,
            alpha=alpha,
        )

    ranks = _average_ranks([abs(d) for d in nonzero])
    w_plus = float(sum(r for d, r in zip(nonzero, ranks) if d > 0))
    w_minus = float(sum(r for d, r in zip(nonzero, ranks) if d < 0))
    m = len(nonzero)

    # One distinct magnitude means every rank is the same averaged value, so
    # W+ is that rank times the number of positive differences and the
    # signed-rank test *is* the sign test. Computed as one, exactly, at any m.
    #
    # This case is not academic: it is what a binary endpoint produces, so the
    # single-run design a replicate study is compared against lands here every
    # time. It is also where the normal approximation is worst — the statistic
    # moves in steps of (m+1)/2, not 1, so the usual half-unit continuity
    # correction is far too small and the approximation comes out
    # anti-conservative by about 0.04 at m = 60 and does not improve with m.
    if len(set(ranks)) == 1:
        positives = sum(1 for d in nonzero if d > 0)
        return WilcoxonResult(
            n=len(values),
            n_nonzero=m,
            zeros=zeros,
            w_plus=w_plus,
            w_minus=w_minus,
            p_one_sided=mcnemar_exact_p(m - positives, positives, one_sided=True),
            p_two_sided=mcnemar_exact_p(m - positives, positives),
            exact=True,
            alpha=alpha,
        )

    if exact is True or (exact is None and m <= MAX_EXACT_SIGNED_RANK):
        doubled = tuple(sorted(int(round(r * 2)) for r in ranks))
        counts = _signed_rank_counts(doubled)
        target = int(round(w_plus * 2))
        at_least = sum(counts[target:])
        at_most = sum(counts[: target + 1])
        p_one = min(1.0, _over_two_pow(at_least, m))
        p_two = min(1.0, 2 * _over_two_pow(min(at_least, at_most), m))
        return WilcoxonResult(
            n=len(values),
            n_nonzero=m,
            zeros=zeros,
            w_plus=w_plus,
            w_minus=w_minus,
            p_one_sided=p_one,
            p_two_sided=p_two,
            exact=True,
            alpha=alpha,
        )

    mean = m * (m + 1) / 4
    tie_correction = 0.0
    seen: dict[float, int] = {}
    for r in ranks:
        seen[r] = seen.get(r, 0) + 1
    for size in seen.values():
        if size > 1:
            tie_correction += size**3 - size
    variance = m * (m + 1) * (2 * m + 1) / 24 - tie_correction / 48
    if variance <= 0:
        p_one = 1.0
    else:
        z = (w_plus - mean - 0.5) / math.sqrt(variance)
        p_one = 0.5 * math.erfc(z / math.sqrt(2))
    return WilcoxonResult(
        n=len(values),
        n_nonzero=m,
        zeros=zeros,
        w_plus=w_plus,
        w_minus=w_minus,
        p_one_sided=min(1.0, p_one),
        p_two_sided=min(1.0, 2 * min(p_one, 1 - p_one)),
        exact=False,
        alpha=alpha,
    )


def render_wilcoxon(
    result: WilcoxonResult, label_a: str = "A", label_b: str = "B"
) -> str:
    """The only sanctioned way to print a signed-rank result.

    Counts first, then the statistic, then p — the same order and the same
    reason as `render_paired`.
    """
    lines = [
        f"n = {result.n} paired items   ({label_a} vs {label_b}, per-item rate difference)",
        f"  differences  : {result.n_nonzero} non-zero, {result.zeros} exactly zero (dropped)",
        f"  signed ranks : W+ = {result.w_plus:g}   W- = {result.w_minus:g}",
        f"  Wilcoxon {'exact' if result.exact else 'normal approx'}  "
        f"one-sided p = {result.p_one_sided:.4f}   "
        f"two-sided p = {result.p_two_sided:.4f}",
    ]
    if not result.exact:
        lines.append(
            "  accuracy     : normal approximation — heavily tied differences put "
            "this within about 0.01 of the exact p, and it does not improve with n. "
            "Recompute with exact=True before deciding anything this close to alpha."
        )
    if result.n_nonzero == 0:
        lines.append(
            "  power        : every paired difference was exactly zero — this "
            "comparison produced no evidence, not a null result"
        )
    return "\n".join(lines)


def replicate_power(
    n: int,
    k: int,
    delta: float,
    baseline_rates: Sequence[float],
    trials: int = 1000,
    seed: int = 20260904,
    alpha: float = 0.05,
) -> float:
    """Simulated power of the replicate design. A projection, not a measurement.

    There is no closed form for Wilcoxon's power, so this is Monte Carlo over a
    **stated** generative model, seeded so it reproduces exactly:

      * each of `n` items draws a baseline pass probability from
        `baseline_rates`, which the caller supplies and must argue for;
      * the candidate arm's probability for that item is the baseline plus
        `delta`, clipped to 1 — a uniform additive effect, which is the
        simplest assumption and the one that has to be written down rather
        than absorbed;
      * each arm runs the item `k` times, and the endpoint is the difference in
        pass fractions;
      * the differences go to `wilcoxon_signed_rank`, one-sided at `alpha`.

    The number that comes out is only as good as `baseline_rates`. It is a
    projection under an assumption, and every document quoting it says so.
    """
    if n <= 0 or k <= 0:
        raise ValueError("replicate_power needs at least one item and one replicate")
    if not baseline_rates:
        raise ValueError("replicate_power needs a baseline rate distribution to draw from")
    if trials <= 0:
        raise ValueError("replicate_power needs at least one simulated trial")
    rng = random.Random(seed)
    rates = list(baseline_rates)
    hits = 0
    for _ in range(trials):
        differences = []
        for _ in range(n):
            p = rng.choice(rates)
            q = min(1.0, p + delta)
            a = sum(1 for _ in range(k) if rng.random() < p) / k
            b = sum(1 for _ in range(k) if rng.random() < q) / k
            differences.append(b - a)
        if wilcoxon_signed_rank(differences, alpha).significant_one_sided:
            hits += 1
    return hits / trials
