# Does a trace ID make a cross-machine failure faster to diagnose?

**Status:** deferred, not run. Written in Theme 4B so it can be run against the
internet-reachable orchestrator once external nodes exist.

**Nothing in this repository claims an improvement in diagnosis time.** No such
measurement has been made. Theme 4B built the mechanism and demonstrated that a
single trace ID correlates the coordinator's view of five induced failures
(`tests/test_trace_correlation.py`); whether that actually helps a human
diagnose faster is a different claim, and this document exists so it is either
measured or left unclaimed.

## Why it cannot be run yet

The comparison needs failures that span machines. On one machine, the "before"
workflow - reading `/events` - is already adequate, because there is only one
event stream and one clock. The thing a trace is supposed to fix is a worker on
somebody else's hardware timing out, being reassigned, and settling late, where
the events on each side have to be lined up by hand.

`scripts/wan_bench.py` reaches a machine in Germany from Indiana, so the network
half exists. What does not exist is a second *operator*, a running remote worker
population, and a person who did not write the code to do the diagnosing.

## What to induce

The same five failures the automated fixtures cover, so the experiment and the
tests describe the same incidents:

| # | Failure | How to induce it |
| --- | --- | --- |
| 1 | Lease expiry | Suspend the worker process mid-task past its lease |
| 2 | Reassignment | Let 1 complete, then let another node take the reclaimed unit |
| 3 | Settlement rejection | Submit with a stale nonce after a coordinator restart |
| 4 | Persistence failure | Fill the coordinator's state volume during settlement |
| 5 | Disconnect mid-stream | Drop the worker's network after its first token batch |

Each is induced by someone other than the diagnoser, at a time the diagnoser
does not know, in a randomised order.

## What to measure

**Primary:** wall-clock seconds from "you have been told something is wrong with
job X" to a written statement naming the failure, the machine, and the point in
the lifecycle - scored against the induction log as correct or not. A wrong
answer is not a fast answer; record it as a failure to diagnose, not as a time.

**Secondary, and worth as much:** how many distinct surfaces the diagnoser had
to open, and whether they reached a wrong conclusion first.

## Arms

* **A - events only.** `tracing_enabled` false. `/events`, `/history`,
  `/run/{id}`, the dashboard, the node pages.
* **B - events plus traces.** `tracing_enabled` true, spans exported to a
  collector, everything in arm A still available.

Same diagnoser sees each failure class once per arm, with arm order alternated
between failure classes so learning does not accumulate in one arm's favour.
Two diagnosers minimum; neither may be the person who wrote the tracing code,
because the author knows which span names exist.

## Size the arms first

ROADMAP section 6 records the lesson from the ensemble comparison: a ten-run
baseline caps what any comparison can prove, and 22 trials could not clear
p<0.05. Diagnosis times vary more than success rates do, so **size both arms
before running either**. Five failure classes times two arms times two
diagnosers is 20 observations - enough to see a large effect, not enough to
resolve a small one. Say which of those the result supports before collecting
it.

## What counts as no improvement

Stated in advance so the result cannot be reinterpreted afterwards:

* Median diagnosis time in arm B is not lower than arm A.
* Or it is lower, but the confidence interval spans zero at the arm sizes
  actually run.
* Or the surface count does not fall - the diagnoser opened the trace *and*
  everything they would have opened anyway, which means tracing added a step.
* Or correctness falls: arm B produced faster answers that were more often
  wrong. That is worse than no improvement, and it is a plausible outcome for a
  view that looks authoritative.

**If any of those holds, it gets published.** ROADMAP section 2: publish the
number that makes us look worse. A tracing feature that did not help is a useful
thing to know about a tracing feature.

## Prerequisites

1. At least two independently operated nodes, whose operators consented to the
   experiment - inducing failures on somebody's machine is not covered by
   consenting to run a worker.
2. A collector the operator runs. Arm B needs export on, which means the
   contributor decides separately whether their worker exports (ADR 0018). A
   worker that declines still produces a usable coordinator-side trace, and the
   experiment should record which arm B nodes exported and which did not.
3. An induction log kept by someone other than the diagnoser.

## A single-worker WAN trace, before any of this

Cheaper and worth doing first: one remote worker, one job, tracing on, and check
that the coordinator's spans for that job share one trace ID across a real
216 ms link. That tests nothing about diagnosis time and everything about
whether propagation survives a real network - and it needs one consenting
operator rather than an experiment.

It was **not run in Theme 4B**: no remote worker was running, and standing one
up means asking someone to install software on their machine, which is not a
thing to do to make a test pass.
