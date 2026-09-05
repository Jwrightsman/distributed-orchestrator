"""Getting a shared admission code from a person into one header, and nowhere else.

`worker_installer.py` already refuses to take an invitation code on the command
line. `join.py` and `node.py` predate it and still accept `--secret VALUE`,
which puts the code somewhere two other people can read it:

* **`ps`.** On Linux and macOS every user on the machine can read every other
  process's argument vector. `ps aux` is not a privileged command.
* **Shell history.** `bash`, `zsh`, and PowerShell all write the line to a file
  in the user's home directory, where it stays until they notice.

Neither is a bug in this project — it is what an argument vector is. So the
argument stays, because deleting it would break setups that already script it,
and two ways in that are not an argument vector are added beside it:

* ``--secret-file PATH`` reads the code from a file, checked by
  `worker_identity.read_owner_only_text` — the same check that guards the
  identity file and the installer's invitation file. There is deliberately no
  second copy of that check anywhere; a second copy is how one of them ends up
  laxer than the other.
* ``--ask-secret`` types it at a prompt with the echo turned off.

Prompting is opt-in rather than automatic, and that is not laziness. A worker
that has already enrolled needs **no** admission code at all — it authenticates
with its own revocable credential — and a coordinator that never set a
`node_secret` needs none either. An unconditional prompt would stop both of
those dead, waiting on a question with no answer.

Nothing here logs, echoes, stores, or returns the code anywhere but to its
caller, which hands it to `node.register` as one header on one request.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from rich.console import Console

from worker_identity import WorkerIdentityError, read_owner_only_text


#: Warnings go to stderr so that redirecting a worker's output to a log file
#: does not quietly swallow the one line saying a credential was exposed.
_console = Console(stderr=True)


class AdmissionSecretError(RuntimeError):
    """The admission code could not be obtained safely."""


#: Printed whenever `--secret` is used. It names the exposure rather than
#: gesturing at it, because "insecure" means nothing to somebody who has not
#: heard of `ps`.
ARGV_SECRET_WARNING = (
    "--secret puts your invitation code in this command's arguments.\n"
    "  Every other user on this machine can read it there with 'ps', and your "
    "shell has already written the whole line to its history file.\n"
    "  Safer, and they do the same thing:\n"
    "    --secret-file PATH   read it from a file only you can read\n"
    "    --ask-secret         type it at a prompt, with nothing shown on screen\n"
    "  Or run 'python worker_installer.py', which never accepts a code on the "
    "command line at all.\n"
    "  You only need an invitation code the first time this machine joins. "
    "After that it has its own credential and needs none."
)

_SECRET_HELP = (
    "Shared node-admission secret. VISIBLE TO OTHER USERS ON THIS MACHINE "
    "through 'ps', and saved in your shell history. Prefer --secret-file or "
    "--ask-secret; kept for setups that already script it"
)

_SECRET_FILE_HELP = (
    "Read the node-admission secret from this file, which must be readable "
    "only by you. Takes a path, never the secret itself"
)

_ASK_SECRET_HELP = (
    "Ask for the node-admission secret at a prompt, with the echo turned off, "
    "so it reaches neither the argument list nor your shell history"
)


def add_admission_secret_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the three mutually exclusive ways to supply an admission code."""

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--secret", default="", help=_SECRET_HELP)
    group.add_argument("--secret-file", type=Path, default=None, help=_SECRET_FILE_HELP)
    group.add_argument("--ask-secret", action="store_true", help=_ASK_SECRET_HELP)


def warn_if_argv_secret(secret: str) -> bool:
    """Say plainly what `--secret` just did. Returns whether it warned."""

    if not secret:
        return False
    _console.print(f"[yellow bold]Warning:[/yellow bold] {ARGV_SECRET_WARNING}")
    return True


def _read_secret_file(secret_file: Path) -> str:
    try:
        value = read_owner_only_text(secret_file)
    except WorkerIdentityError as exc:
        raise AdmissionSecretError(
            f"that secret file cannot be used: {exc}. It must be a plain file "
            f"that only you can read — on macOS or Linux: chmod 600 {secret_file}"
        ) from None
    if not value:
        raise AdmissionSecretError(
            f"the secret file {secret_file} is empty; it should contain the "
            "invitation code and nothing else"
        )
    return value


def _prompt_for_secret() -> str:
    if not sys.stdin.isatty():
        raise AdmissionSecretError(
            "--ask-secret needs a terminal to ask at, and there is not one here. "
            "Put the code in a file only you can read and pass --secret-file PATH "
            "instead."
        )
    _console.print("[dim]Nothing will appear as you type — that is normal.[/dim]")
    try:
        value = getpass.getpass("Invitation code: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise AdmissionSecretError("cancelled at the invitation-code prompt") from None
    if not value:
        raise AdmissionSecretError("no invitation code was entered")
    return value


def resolve_admission_secret(
    *,
    secret: str = "",
    secret_file: Path | None = None,
    ask: bool = False,
    warn: bool = True,
) -> str:
    """Return the admission code from whichever source was chosen, or ``""``.

    An empty string is a real answer, not a failure: a returning worker and a
    coordinator with no configured `node_secret` both register without one.
    """

    if secret:
        if warn:
            warn_if_argv_secret(secret)
        return secret
    if secret_file is not None:
        return _read_secret_file(Path(secret_file))
    if ask:
        return _prompt_for_secret()
    return ""


def resolve_from_args(args: argparse.Namespace, *, warn: bool = True) -> str:
    """`resolve_admission_secret` for a namespace built by the adder above."""

    return resolve_admission_secret(
        secret=getattr(args, "secret", "") or "",
        secret_file=getattr(args, "secret_file", None),
        ask=bool(getattr(args, "ask_secret", False)),
        warn=warn,
    )
