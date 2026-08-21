"""A fingerprint of the code this process is actually running.

Why this exists: **a deploy that did nothing looks exactly like one that
worked.** It happened here — the live orchestrator's `git pull` failed with
"not a git repository", the `&&` chain stopped before the rebuild, and the
deploy printed nothing alarming. The WebSocket bug it was meant to fix stayed
broken for a day, and the only tell was the container's uptime.

Version strings don't help, because nobody remembers to bump them. Git doesn't
help either: `.git` is not copied into the image (see the Dockerfile), so the
server has no way to ask which commit it came from, and asking the *host* only
proves the checkout moved — which is exactly the half of the deploy that
already worked in the failure above.

So: hash the source files the server is running, from inside the running
process. It cannot be stale, cannot be faked by a successful `git pull`, and it
answers the only question that matters — is the code serving requests right now
the same code I have here?

Line endings are normalised because a Windows checkout stores `\\r\\n` and the
Linux image stores `\\n`; without that, the same code fingerprints differently
on either side and the check cries wolf.
"""

import hashlib
from pathlib import Path

# Exactly what the Dockerfile copies into the image. Keep this list in step
# with it — a file the image has and this list doesn't is a file whose change
# would not show up in the fingerprint.
_APP_DIR = Path(__file__).resolve().parent
_INCLUDED = ("*.py", "templates/*", "prompts/*.py", "execution/*.py")


def _source_files() -> list[Path]:
    seen: set[Path] = set()
    for pattern in _INCLUDED:
        for path in _APP_DIR.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                seen.add(path)
    return sorted(seen)


def fingerprint() -> str:
    """Short hash over the server's own source. Same input, same 12 characters."""
    digest = hashlib.sha256()
    for path in _source_files():
        digest.update(path.relative_to(_APP_DIR).as_posix().encode())
        digest.update(b"\0")
        # Normalise line endings so a CRLF checkout and an LF image agree.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


BUILD = fingerprint()
