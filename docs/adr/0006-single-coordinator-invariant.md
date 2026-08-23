# ADR 0006: One Coordinator Owns One State Directory

- Status: Accepted
- Date: 2026-08-23
- Decision scope: trusted-alpha coordinator deployment and recovery

## Context

Mycelium persists executions, attempt authority, contributions, artifact
manifests, and shares in one SQLite database, but its worker queue, dispatcher
waits, connected-node registry, background tasks, and node sessions remain
process-local. Two coordinator processes pointed at the same directory could
each believe it owns scheduling and cleanup while observing only part of the
other's memory.

SQLite WAL and busy handling serialize database writes; they cannot reconcile
two independent schedulers, cancel the other process's coroutine, preserve one
node-session registry, or decide which process may prune an active artifact
root. A conventional PID file is also insufficient: PIDs are reused, stale
files survive crashes, and checking then writing is racy.

The trusted alpha needs a simple invariant that works on supported local filesystems
on Linux and Windows, fails before state mutation, and recovers automatically
after process death. It does not need coordinator high availability in RC1.

## Decision

Exactly one coordinator process may own one state directory.

At FastAPI lifespan startup, before database migration, restart
reconciliation, or background cleanup, the process:

1. rejects common explicit multi-worker configurations;
2. opens `.mycelium-coordinator.lock` inside the state directory;
3. attempts a nonblocking exclusive operating-system lock;
4. fails startup with an operator-facing error if another owner holds it;
5. writes bounded diagnostic metadata while holding the lock; and
6. retains the open locked handle for the entire application lifespan.

POSIX uses `flock(LOCK_EX | LOCK_NB)`. Windows uses `msvcrt.locking` on one
byte. The kernel lock, not file contents, decides ownership. Metadata contains a
random instance ID, PID, UTC start time, and deployment mode so a private
operator health response can identify the running process. It contains no
credential values.

The lock file is not unlinked during ordinary shutdown. Removing a locked path
can create a second inode and a split ownership check on POSIX. Instead, the
handle is unlocked/closed; the next coordinator opens the same persistent path
and replaces its diagnostic metadata only after acquiring ownership.

Process exit, including an ungraceful death, closes the handle and releases the
kernel lock. A replacement may then start and perform normal restart
reconciliation. It does not resume the dead process's scheduler state.

The Docker image launches Uvicorn without worker fan-out. Compose mounts one
state directory, declares its config/state paths, and defaults port publishing
to loopback. `WEB_CONCURRENCY`, `UVICORN_WORKERS`, Uvicorn/Gunicorn `--workers`,
and equivalent detected counts other than one are rejected. The OS lock remains
the final authority when a launch mechanism is not recognized.

## Health and preflight

Offline preflight acquires and releases the same OS lock. Failure means an
owner is active or the filesystem cannot establish the invariant. An operator
may skip only the lock probe to inspect other settings on a deliberately
running coordinator; that result carries a warning and is not proof the lock
would be available after shutdown.

Private `GET /v1/operator/health` reports `instance_id`, `deployment_mode`,
`single_coordinator_lock`, and sanitized preflight warnings. Public `/health`
does not expose PID or state paths.

## Consequences

- Migrations, reconciliation, cleanup, scheduler memory, and artifact active
  roots have one process owner.
- An accidental second server fails clearly instead of partially working.
- Crash recovery does not require deleting a stale PID file.
- Tests can run independent coordinators by using independent state
  directories.
- One machine cannot scale coordinator HTTP throughput with multiple Uvicorn
  workers over shared state.
- Planned maintenance and failover require stopping the old owner before
  starting the replacement.
- The lock is only as reliable as the underlying OS/filesystem advisory-lock
  implementation. Network/distributed filesystems with uncertain lock
  semantics are unsupported.
- This decision does not make the in-memory scheduler durable and does not add
  high availability.

## Operator recovery

If startup reports an owner:

1. verify the intended coordinator with process/container status and private
   operator health;
2. stop that process normally;
3. wait for it to exit and run full preflight again; and
4. start one replacement.

Do not delete the lock file, kill a process solely because the diagnostic PID
looks stale, or use `--force` to bypass ownership. If no process is alive but
the lock remains unavailable, investigate the filesystem/mount; the persistent
file's existence by itself is normal.

## Rejected alternatives

### PID file ownership

Rejected because existence and PID contents are not atomic ownership, survive
crashes, and are vulnerable to PID reuse and check/write races.

### Rely on SQLite WAL alone

Rejected because database serialization cannot coordinate process-local queues,
node sessions, cleanup, cancellation, and artifact lifecycle.

### Database lease row

Rejected for RC1 because a correct lease requires clock/renewal/fencing design
and still permits two processes during lease ambiguity unless every mutation
checks a fencing token. The local OS already provides automatic process-death
release for the supported deployment.

### Allow multiple read-only API workers

Rejected because current application imports share mutable process state and
routes are not separated into stateless reader and scheduler roles. A partial
multi-process mode would be difficult to explain and easy to misconfigure.

### Distributed leader election and high availability

Deferred. HA requires durable scheduler semantics, fencing, shared artifact
storage, node-session continuity decisions, and tested failover. Claiming it
from a file lock would be misleading.
