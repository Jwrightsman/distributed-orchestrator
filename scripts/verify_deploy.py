"""Did the deploy actually land? Answers in one word.

    python scripts/verify_deploy.py http://167.233.239.33:8000

A redeploy that silently did nothing looks exactly like one that worked. That
happened here: `git pull` failed with "not a git repository", the `&&` chain
stopped before the rebuild, and nothing in the output said so. The bug it was
meant to fix stayed live for a day.

This compares the fingerprint of the code in *this* checkout with the
fingerprint the *running server* reports for the code it is actually executing
(`build` in /status.json). They match or they don't. No version strings to
forget to bump, no reading a commit off the host that only proves the checkout
moved.

Run it from a clean checkout of the branch you deployed:

    git checkout master && git pull
    python scripts/verify_deploy.py http://YOUR_SERVER:8000

Exit code 0 means the live server is running this code. 1 means it is not.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from build_info import BUILD  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")

    print(f"this checkout : {BUILD}")
    try:
        resp = httpx.get(f"{base}/status.json", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"live server   : UNREACHABLE ({type(exc).__name__}: {exc})")
        print()
        print("Could not reach the server at all. Check the address, and that the")
        print("container is running:  docker compose ps")
        return 1

    live = data.get("build")
    if live is None:
        print("live server   : (no build field)")
        print()
        print("STALE - the running code predates deploy verification entirely.")
        print("The server is serving an image built before build_info.py existed,")
        print("which means the redeploy did not rebuild. Re-run the deploy and")
        print("check the errors this time:")
        print()
        print('  ssh -i ~/.ssh/swarm_orchestrator root@SERVER \\')
        print('    "cd /root/distributed-orchestrator && git pull && '
              'docker compose up -d --build"')
        return 1

    print(f"live server   : {live}")
    print(f"uptime        : {data.get('uptime_seconds', '?')}s")
    print(f"nodes online  : {data.get('nodes_online', '?')}")
    print()

    if live == BUILD:
        print("MATCH - the live server is running exactly this code.")
        return 0

    print("STALE - the live server is running different code from this checkout.")
    print()
    print("Most likely the rebuild did not happen. Two things to check on the box:")
    print("  docker compose ps      # orchestrator uptime should be seconds, not days")
    print("  git -C /root/distributed-orchestrator log --oneline -1")
    print()
    print("If git says 'not a git repository', that is the known tarball trap -")
    print("docs/DEPLOY.md has the one-time conversion procedure.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
