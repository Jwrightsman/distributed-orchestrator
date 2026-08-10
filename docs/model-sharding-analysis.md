# Why this project does *not* shard one model across machines

A reasonable question about a distributed AI project: why run many small models
instead of splitting one big model across the machines? "Four laptops, none of
which can run a 30B model, running one together" is a better headline than what
this project does.

We evaluated it and decided against it. Here is the reasoning, with the numbers
that produced it, so you can check the working rather than take our word.

## What sharding would mean here

[llama.cpp's RPC backend](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md)
splits a model's layers across machines. Each machine runs `rpc-server`, and the
coordinator ships **every tensor operation** to whichever machine holds that
layer, waits for the result, and continues. Setup is genuinely simple: build
with RPC enabled, start the workers, pass `--rpc host:port,host:port`.

The catch is in that word *every*. A forward pass crosses the network once per
layer boundary. A 40-layer model split in half crosses at least once per token,
and the crossing carries activation tensors, not a few bytes.

## Why it fails on this project's actual hardware

We measured our real link (`scripts/wan_bench.py`, laptop in Indiana →
orchestrator in Germany):

| | measured |
| --- | --- |
| HTTP round-trip | **216 ms** |
| 8 KB payload upload | 535 ms |

The published guidance for llama.cpp RPC is **10GbE as the minimum viable
interconnect**, with InfiniBand for anything serious. Sub-millisecond, in other
words. Our link is 216 ms — roughly *three orders of magnitude* outside the
requirement. At one crossing per token, that alone is minutes per token before
any compute happens.

This is not a tuning problem. Sharding assumes machines sitting on the same
switch. This project assumes machines sitting in different houses.

## The deeper point: the two designs solve different problems

Sharding exists to answer **"this model doesn't fit in my RAM."** It is slower
than running the model on one machine that can hold it — you accept that cost to
run something you otherwise couldn't run at all.

This project answers a different question: **"a task is bigger than one model
call."** It splits the *work*, not the *weights*. Each machine runs a whole
model on a whole subtask, independently, and only the finished text crosses the
network.

That difference is why the WAN numbers land where they do. We measured the
network at **~2% of a real pitch's wall clock** (7 s of 308 s). Subtask-level
distribution is latency-tolerant almost to the point of indifference; a worker
across an ocean is as useful as one on your LAN. Tensor-level distribution is
the opposite — it is latency-*bound*, and the ocean makes it unusable.

Neither is better. They are answers to different questions, and picking the
wrong one for your topology gives you something that technically runs and is
practically useless.

## When sharding *would* make sense here

If you have several machines on one fast LAN and want the swarm to run a model
larger than any single machine holds, sharding is the right tool — and it would
compose with this project rather than replace it: a sharded cluster could
register as one powerful node. That is a real future direction, not a
contradiction.

What makes it wrong *today* is our topology, not the idea.

## Status

Not implemented. Not planned before launch. Revisit if the network becomes a
set of LAN clusters rather than scattered individual machines.
