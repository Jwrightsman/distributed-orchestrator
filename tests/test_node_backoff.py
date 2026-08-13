"""Reconnect backoff — SPRINT_PHASE2 §3, "harden anything the WAN test exposes".

The WAN benchmark found latency was never the problem: registration is 218 ms
against a 10 s timeout, and a real pitch spends ~2% of its life on the network.
What it did expose is the retry *pattern*. The node slept a flat 10 s forever,
so when the orchestrator restarts — a redeploy, exactly what happened on Aug 12
— every connected node drops at the same instant and retries in lockstep
against a box that is still booting.
"""

import random

import node


def test_first_retry_is_quick():
    """A brief blip should not cost a node a minute of work."""
    for seed in range(50):
        assert node.reconnect_delay(0, random.Random(seed)) <= 3.0


def test_backoff_grows_with_consecutive_failures():
    """Averaged over jitter, later attempts wait longer."""
    def mean(attempt):
        return sum(node.reconnect_delay(attempt, random.Random(s)) for s in range(200)) / 200

    means = [mean(a) for a in range(5)]
    assert means == sorted(means), means
    assert means[4] > means[0] * 4


def test_never_exceeds_the_cap():
    """Jitter used to be applied after the cap, so a 60 s cap could yield 75 s."""
    worst = max(
        node.reconnect_delay(attempt, random.Random(seed))
        for attempt in range(12)
        for seed in range(150)
    )
    assert worst <= node._RECONNECT_CAP_S


def test_cap_holds_for_very_large_attempt_counts():
    """A node offline overnight must not compute an absurd delay or overflow."""
    for attempt in (50, 500, 5000):
        delay = node.reconnect_delay(attempt, random.Random(1))
        assert 0 < delay <= node._RECONNECT_CAP_S


def test_negative_attempts_are_treated_as_the_first():
    assert 0 < node.reconnect_delay(-5, random.Random(1)) <= 3.0


def test_jitter_desynchronises_simultaneous_reconnects():
    """The point of the change.

    Fifty nodes dropping together must not all come back at the same moment.
    Without jitter every one of these would be identical.
    """
    delays = [node.reconnect_delay(3, random.Random(seed)) for seed in range(50)]
    assert len(set(round(d, 3) for d in delays)) > 40
    assert max(delays) - min(delays) > 1.0
