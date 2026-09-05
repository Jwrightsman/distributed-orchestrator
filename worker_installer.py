"""A guided worker install for somebody who has not used a terminal much.

Joining used to mean: clone a repository, install Python and Ollama, pull a
model, run a script, and paste a shared secret into a flag that lands in shell
history and in `ps`. Each of those is a place to get it wrong quietly. This is
one command that walks through the same ground, says what it is about to do
before it does it, and refuses rather than guesses.

What it will not do is as much of the point as what it will:

* it never accepts a secret on the command line — an invitation code is typed
  at a prompt with echo off, or read from a file whose permissions are checked;
* it never prints, logs, or writes a secret anywhere but the identity file that
  `worker_identity` already knows how to write atomically and owner-only;
* it never runs as root or Administrator, because nothing here needs to;
* it never touches the machine's firewall, trust store, or security settings;
* it never phones home, checks for updates, or talks to any host except the one
  coordinator it was pointed at and the local Ollama;
* it never builds a shell command out of anything a coordinator sent.

Transport is not negotiable and is not decided here: `worker_transport` refuses
plaintext HTTP to any non-loopback host, and this module simply inherits that
through `normalize_coordinator`.

macOS gets one specific piece of care, because the platform has a state nothing
else does. Ollama ships there as an application, and the background service and
the `ollama` command both come into existence the first time somebody opens it.
So the ordinary state of a Mac two minutes after installing Ollama is:
installed, not running, no command — and an installer that answers that with
"install Ollama" is telling somebody to redo the thing they just did. The
service is asked whether it answers; the command is asked about separately and
only mentioned when it is about to be needed. Nothing here opens the
application, edits a search path, or writes to a shell profile: those are the
contributor's, and doing any of them quietly is how a machine ends up different
from the way its owner left it.

Order matters more than it looks. The consent screen comes before the model
download, not after it, because a 2.5 GB write is itself a thing somebody has
to agree to — ROADMAP §2 makes that a permanent constraint and
`tests/test_worker_installer.py` holds the order in place.

Usage:

    python worker_installer.py              # guided join
    python worker_installer.py uninstall    # drain, then remove the credential
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel

from config import get as get_config
from ollama_client import DEFAULT_MODEL, check_ollama
from worker_identity import (
    WorkerIdentityError,
    create_worker_identity,
    default_identity_file,
    load_worker_identity,
    normalize_coordinator,
    normalize_worker_node_id,
    read_owner_only_text,
)

console = Console()


# ── Exit codes ───────────────────────────────────────────────────────────────
#
# A person who cannot read a traceback still needs to be able to say what
# happened, and a script wrapping this needs to be able to branch on it. Each
# failure gets its own number and a sentence of plain English.

EXIT_OK = 0
EXIT_PRIVILEGED = 3
EXIT_PLATFORM = 4
EXIT_OLLAMA_MISSING = 5
EXIT_MODEL_FAILED = 6
EXIT_BAD_COORDINATOR = 7
EXIT_DECLINED = 8
EXIT_IDENTITY = 9
EXIT_PROTOCOL_MISMATCH = 10
EXIT_REGISTRATION = 11
EXIT_UNREACHABLE = 12
EXIT_INTERRUPTED = 130

#: The oldest Python this worker is known to run on. CI runs 3.14, which is what
#: any claim about this project actually rests on; 3.12 is where `asyncio` stops
#: quietly creating an event loop, and below that the worker misbehaves subtly
#: rather than loudly.
MINIMUM_PYTHON = (3, 12)
CI_PYTHON = (3, 14)

SUPPORTED_SYSTEMS = ("Linux", "Darwin", "Windows")

OLLAMA_INSTALL_URL = "https://ollama.com/download"
PROJECT_URL = "https://github.com/Jwrightsman/distributed-orchestrator"

#: Where the macOS disk image puts Ollama. This is read, never opened: starting
#: an application on somebody's behalf is not this program's business, and the
#: whole point of the macOS message below is to hand that decision back to them.
MACOS_OLLAMA_APP = Path("/Applications/Ollama.app")

#: A model name is data. It reaches `ollama pull` as one element of an argument
#: list and never as shell text, but it is also checked against this, so a value
#: that could only have come from somewhere it should not have is refused before
#: any process is started.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")

_HTTP_TIMEOUT = 15.0


class InstallerError(RuntimeError):
    """A step failed for a reason the contributor can be told plainly."""

    def __init__(self, message: str, exit_code: int, *, hint: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint


# ── Presentation ─────────────────────────────────────────────────────────────

_STEP_TOTAL = 9


def _step(number: int, title: str) -> None:
    console.print(f"\n[bold cyan]Step {number}/{_STEP_TOTAL}[/bold cyan]  {title}")


def _ok(message: str) -> None:
    console.print(f"  [green]OK[/green]  {message}")


def _info(message: str) -> None:
    console.print(f"      [dim]{message}[/dim]")


# ── Step 1: privileges ───────────────────────────────────────────────────────

def running_privileged() -> bool:
    """Is this process root or an elevated Administrator?

    Nothing the installer does needs it: the model lives in the user's Ollama
    store, and the identity file lives in the user's own configuration
    directory. A file written as root is a file the contributor then cannot
    read, which is the friendly reason; the unfriendly reason is that an
    installer somebody ran with sudo is an installer that can do anything.
    """

    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None:
        return geteuid() == 0
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            # Unable to tell. Say "not elevated" rather than blocking a join on
            # a probe that failed; the destructive-privilege case this guards is
            # POSIX sudo, which is detected exactly.
            return False
    return False


def check_privileges() -> None:
    _step(1, "Checking that this is not running with administrator rights")
    if running_privileged():
        raise InstallerError(
            "This is running as root or Administrator, and it will not continue.",
            EXIT_PRIVILEGED,
            hint=(
                "Nothing here needs those rights. Close this window and run the "
                "same command again from your normal account — without sudo, and "
                "not from an elevated terminal."
            ),
        )
    _ok("running as an ordinary user")


# ── Step 2: platform and Python ──────────────────────────────────────────────

def describe_machine(system: str, machine: str) -> str:
    """Name the processor family the way its owner would name it.

    "arm64" is what `platform.machine()` says on an Apple Silicon Mac and it is
    not what anybody calls their computer. The distinction is worth drawing
    because it is the one hardware fact that changes what to expect: Ollama
    uses Metal on Apple Silicon and the CPU on an Intel Mac, and the honest
    numbers this project publishes were measured CPU-only.
    """

    normalized = (machine or "").strip().lower()
    if system == "Darwin":
        if normalized in {"arm64", "aarch64"}:
            return f"Apple Silicon ({machine}) — Ollama uses the GPU here"
        if normalized in {"x86_64", "amd64"}:
            return f"Intel ({machine}) — inference runs on the processor"
    return machine or "unknown"


def check_platform(
    *,
    system: str | None = None,
    version: tuple[int, ...] | None = None,
    machine: str | None = None,
) -> None:
    _step(2, "Checking this computer and its Python version")
    running_system = platform.system() if system is None else system
    running_version = tuple(sys.version_info[:2]) if version is None else tuple(version[:2])
    running_machine = platform.machine() if machine is None else machine

    if running_system not in SUPPORTED_SYSTEMS:
        raise InstallerError(
            f"Mycelium workers have not been tested on {running_system or 'this system'}.",
            EXIT_PLATFORM,
            hint=f"Supported: Linux, macOS, Windows. See {PROJECT_URL}",
        )
    if running_version < MINIMUM_PYTHON:
        running = ".".join(str(part) for part in running_version)
        needed = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise InstallerError(
            f"This is Python {running}, and the worker needs {needed} or newer.",
            EXIT_PLATFORM,
            hint=(
                f"Install a newer Python from https://www.python.org/downloads/ "
                f"and run this again. The project is tested on "
                f"{'.'.join(str(part) for part in CI_PYTHON)}."
            ),
        )
    _ok(f"{running_system}, Python {'.'.join(str(part) for part in running_version)}")
    _info(f"Processor: {describe_machine(running_system, running_machine)}")
    # The model is not architecture-specific — Ollama publishes one tag and
    # serves the right build for the machine asking — so there is nothing to
    # verify here that step 7 does not verify for real by pulling it. Saying
    # which processor was detected is what the contributor can act on; claiming
    # the model "is available for" it would be a claim nothing checked.
    if running_version < CI_PYTHON:
        _info(
            f"Tested on Python {'.'.join(str(part) for part in CI_PYTHON)}; "
            "yours is older but supported."
        )


# ── Step 3: Ollama ───────────────────────────────────────────────────────────

def ollama_cli_available() -> bool:
    """Is the `ollama` command visible to this terminal?

    Deliberately separate from "is Ollama running". On macOS these come apart:
    Ollama ships as an application, and the command-line tool is a symbolic
    link the application creates the first time somebody opens it.
    """

    return shutil.which("ollama") is not None


def macos_ollama_app_present(
    *, system: str | None = None, app_path: Path | None = None
) -> bool:
    """Is the Ollama application sitting in /Applications on this Mac?"""

    if (platform.system() if system is None else system) != "Darwin":
        return False
    try:
        return (MACOS_OLLAMA_APP if app_path is None else app_path).exists()
    except OSError:
        return False


def ollama_absence_hint(*, system: str | None = None, app_present: bool | None = None) -> str:
    """What to tell somebody whose Ollama this program could not reach.

    Three different situations, and telling somebody to install software they
    have already installed is how a working machine gets read as a broken one:

    * it is not installed — say where to get it;
    * it is installed on a Mac but has never been opened — say *that*, because
      on macOS the background service starts when the application does, and
      somebody who just double-clicked the disk image has done everything they
      were told to and is looking at a message saying to do it again;
    * anywhere else — say how to start it.
    """

    running = platform.system() if system is None else system
    installed = macos_ollama_app_present(system=running) if app_present is None else app_present

    if running == "Darwin" and installed:
        return (
            "Ollama is already installed on this Mac — it just has not been "
            "opened yet, and it only starts working once it has been.\n"
            "  1. Open your Applications folder and double-click Ollama (or "
            "press Command-Space, type 'Ollama', and press Return).\n"
            "  2. A small llama appears in the menu bar along the top of the "
            "screen. That means it is running. There is no window to keep open.\n"
            "  3. Come back to this terminal and run this again.\n"
            "You only have to do this once; from then on it starts with your Mac."
        )
    if running == "Darwin":
        return (
            f"Download it from {OLLAMA_INSTALL_URL}, drag it into Applications, "
            "and then open it once — a small llama appears in the menu bar when "
            "it is running. Then run this again.\n"
            "This installer will not install it for you — putting software on "
            "your machine without you watching is not something it should do."
        )
    return (
        f"Install it from {OLLAMA_INSTALL_URL}, then start it (on Linux: "
        "'ollama serve') and run this again.\n"
        "This installer will not install it for you — putting software on "
        "your machine without you watching is not something it should do."
    )


def ollama_cli_missing_hint(*, system: str | None = None) -> str:
    """Ollama answers on the network, but its command is not in this terminal.

    Almost always macOS, and almost always the same cause: the application
    creates its command-line tool the first time it is opened, and a terminal
    opened before that has an older idea of where to look. The fix belongs to
    the contributor — this program does not edit anybody's shell configuration
    or their search path, and says so instead of doing it quietly.
    """

    running = platform.system() if system is None else system
    if running == "Darwin":
        return (
            "Ollama is running, but this terminal cannot find its 'ollama' "
            "command. On a Mac the application creates that command the first "
            "time it is opened.\n"
            "  1. Open Ollama from your Applications folder if you never have.\n"
            "  2. Close this terminal window and open a new one — a window "
            "opened earlier does not pick the command up.\n"
            "  3. Type 'ollama --version'. When that answers, run this again.\n"
            "This installer will not change your search path or edit your shell "
            "settings on your behalf."
        )
    return (
        "Ollama is installed somewhere this terminal cannot see. "
        "Close and reopen the terminal, then try again."
    )


async def check_ollama_present() -> list[str]:
    """Confirm Ollama is installed and running. Never install it.

    The question asked is whether the local service *answers*, not whether a
    command exists — the service is what does the work, and on macOS the two
    are genuinely independent. The command is checked separately, and only
    mentioned when it is both missing and about to be needed.
    """

    _step(3, "Looking for Ollama, the program that runs the AI model")
    status = await check_ollama()
    if not status.get("ok"):
        raise InstallerError(
            "Ollama is not running on this computer.",
            EXIT_OLLAMA_MISSING,
            hint=ollama_absence_hint(),
        )
    models = [str(name) for name in status.get("models", [])]
    _ok(f"Ollama is running, with {len(models)} model(s) already downloaded")
    if not ollama_cli_available():
        _info(
            "Its 'ollama' command is not on this terminal's path. That only "
            "matters if the model still has to be downloaded."
        )
    return models


# ── Step 4: the coordinator address ──────────────────────────────────────────

def _read_line(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise InstallerError(
            "Nobody is at the keyboard, so there is nobody to ask.",
            EXIT_DECLINED,
            hint=(
                "This installer needs a person present: it is about to commit "
                "this machine's processor to work submitted by other people. Run "
                "it from a terminal window you are sitting in front of."
            ),
        )
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise InstallerError("Cancelled — nothing was changed.", EXIT_INTERRUPTED) from None


def ask_coordinator(supplied: str | None = None) -> str:
    """Ask for, and validate, the address of the coordinator."""

    _step(4, "Asking where to connect")
    raw = supplied if supplied else _read_line(
        "  Paste the address you were given (it should start with https://): "
    )
    if not raw:
        raise InstallerError(
            "No address was given, so there is nothing to join.",
            EXIT_BAD_COORDINATOR,
            hint="Ask whoever invited you for the address of their coordinator.",
        )
    try:
        origin = normalize_coordinator(raw)
    except WorkerIdentityError as exc:
        raise InstallerError(
            f"That address cannot be used: {exc}",
            EXIT_BAD_COORDINATOR,
            hint=(
                "It has to be a plain https:// web address with no path, no "
                "username, and no question mark — for example "
                "https://mycelium.example.com"
            ),
        ) from None
    _ok(f"will connect to {origin}")
    return origin


# ── Step 5: plain-language consent ───────────────────────────────────────────

def consent_text(origin: str, model: str, identity_path: Path) -> str:
    """The paragraph you would want to read before installing a stranger's software."""

    return (
        f"Your computer is about to join [cyan]{origin}[/cyan] as a worker.\n\n"
        "[bold]In ordinary English:[/bold]\n"
        "  Whoever runs that address will send your computer pieces of writing —\n"
        "  prompts — and your computer will run an AI model on them and send the\n"
        "  text back. That is the whole arrangement.\n\n"
        "  They [bold]cannot[/bold] see anything else on your machine. Not your files,\n"
        "  not your other programs, not your browsing. Your computer does not run\n"
        "  anything they send; it only reads it as text and answers it.\n\n"
        "  They [bold]can[/bold] see the answers your computer produces, and they can see\n"
        "  that your machine is connected and how much work it has done.\n\n"
        "  You can stop at any time. Close the window, or drain first if you would\n"
        "  rather finish the piece of work you are holding. One command removes\n"
        "  everything this installer set up. Both are printed at the end.\n\n"
        "[bold]What this costs you:[/bold]\n"
        f"  - about 2.5 GB of disk for the [bold]{model}[/bold] model, if you do not have it\n"
        "  - your processor running flat out, in bursts of minutes, while it works\n"
        "  - 8 GB of memory is the practical minimum\n"
        "  - very little network; a task spends about 2% of its time on the wire\n\n"
        "[bold]What will be written to this computer:[/bold]\n"
        f"  - the model, into Ollama's own store\n"
        f"  - one small file: [dim]{identity_path}[/dim]\n"
        "    It holds a private code that identifies this machine to that\n"
        "    coordinator and nothing else. It is created readable only by you.\n\n"
        "[bold]What will NOT be touched:[/bold]\n"
        "  - your firewall, your certificates, or any security setting\n"
        "  - anything that starts automatically when your computer boots\n"
        "  - no inbound port is opened; your machine dials out, never the reverse\n"
        "  - nothing is reported to anybody except that one coordinator"
    )


def confirm(origin: str, model: str, identity_path: Path) -> None:
    _step(5, "What is about to happen")
    console.print(
        Panel(
            consent_text(origin, model, identity_path),
            title="[bold]Please read this before agreeing[/bold]",
            border_style="yellow",
        )
    )
    answer = _read_line("\n  Type 'yes' to continue, or press Enter to stop: ").lower()
    if answer not in {"y", "yes"}:
        raise InstallerError(
            "Stopped at your request. Nothing was downloaded, written, or joined.",
            EXIT_DECLINED,
        )
    _ok("agreed")


# ── Step 6: the invitation code ──────────────────────────────────────────────

def read_invitation(invitation_file: Path | None) -> str:
    """Obtain the shared invitation code without it ever reaching argv.

    Two ways in, and neither is a command-line argument: typed at a prompt with
    the echo turned off, or read from a file whose permissions are checked
    first by the same code that guards the identity file.
    """

    _step(6, "Asking for your invitation code")
    if invitation_file is not None:
        try:
            secret = read_owner_only_text(invitation_file)
        except WorkerIdentityError as exc:
            raise InstallerError(
                f"That invitation file cannot be used: {exc}",
                EXIT_BAD_COORDINATOR,
                hint=(
                    "It must be a plain file that only you can read. On macOS or "
                    f"Linux: chmod 600 {invitation_file}"
                ),
            ) from None
        if not secret:
            raise InstallerError(
                "The invitation file is empty.",
                EXIT_BAD_COORDINATOR,
                hint="It should contain the invitation code and nothing else.",
            )
        _ok(f"read from {invitation_file}")
        return secret

    if not sys.stdin.isatty():
        raise InstallerError(
            "Nobody is at the keyboard, so the invitation code cannot be typed.",
            EXIT_DECLINED,
            hint=(
                "Run this from a terminal you are sitting at, or put the code in "
                "a file only you can read and pass --invitation-file PATH. It is "
                "never accepted as a command-line argument, because anything on "
                "the command line is visible to every other program on the "
                "machine and is saved in your shell history."
            ),
        )
    console.print(
        "      [dim]Nothing will appear as you type — that is normal.[/dim]"
    )
    try:
        secret = getpass.getpass("  Invitation code: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise InstallerError("Cancelled — nothing was changed.", EXIT_INTERRUPTED) from None
    if not secret:
        raise InstallerError(
            "No invitation code was entered.",
            EXIT_BAD_COORDINATOR,
            hint="Ask whoever invited you for the code that goes with the address.",
        )
    _ok("received (it will not be shown or written down anywhere but your identity file)")
    return secret


# ── Step 7: the model ────────────────────────────────────────────────────────

def _validate_model_name(model: str) -> str:
    if not _MODEL_NAME_RE.fullmatch(model or ""):
        raise InstallerError(
            "The configured model name is not a valid Ollama model name.",
            EXIT_MODEL_FAILED,
            hint="Check the 'model' setting in config.json.",
        )
    return model


async def ensure_model(model: str, present: list[str]) -> str | None:
    """Pull the model if it is missing, then report the digest Ollama holds.

    `subprocess.run` is given an argument list. There is no shell, so there is
    nothing for a model name to escape into even if one ever arrived from
    somewhere it should not have.
    """

    _step(7, f"Making sure the {model} model is on this computer")
    _validate_model_name(model)

    if any(model in candidate for candidate in present):
        _ok("already downloaded")
    else:
        console.print(
            "      [dim]Downloading about 2.5 GB. This can take a while; "
            "progress is Ollama's own.[/dim]"
        )
        try:
            completed = subprocess.run(["ollama", "pull", model], check=False)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            raise InstallerError(
                "The 'ollama' command could not be found, even though Ollama is running.",
                EXIT_MODEL_FAILED,
                hint=ollama_cli_missing_hint(),
            ) from None
        if completed.returncode != 0:
            raise InstallerError(
                f"Downloading {model} failed.",
                EXIT_MODEL_FAILED,
                hint=(
                    "Check that you are online and have about 3 GB free, then run "
                    f"this again. You can also try it directly: ollama pull {model}"
                ),
            )
        _ok("downloaded")

    from node import _detect_ollama_metadata

    _, digest, _variant = await _detect_ollama_metadata(
        model, str(get_config().get("ollama_url") or "http://localhost:11434")
    )
    if digest:
        _ok(f"confirmed, digest {digest[:19]}…")
    else:
        # Not fatal: older Ollama builds do not report one, and the worker
        # advertises a digest only when Ollama actually supplies it.
        _info("Ollama did not report a digest for this model; continuing without one.")
    return digest


# ── Step 8: identity and registration ────────────────────────────────────────

async def fetch_protocol_window(origin: str) -> dict:
    """Read the coordinator's advertised version window before enrolling.

    Unauthenticated by design (ADR 0015), which is why it happens here: if this
    worker is outside the window there is no reason to send an invitation code
    to a coordinator that is going to refuse it.
    """

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, trust_env=False, follow_redirects=False
    ) as client:
        try:
            response = await client.get(f"{origin}/v1/worker-protocol")
        except httpx.HTTPError as exc:
            raise InstallerError(
                f"Could not reach {origin}.",
                EXIT_UNREACHABLE,
                hint=(
                    "Check the address, and check that you are online. If the "
                    "address is right, whoever runs it may have it switched off.\n"
                    f"[technical detail: {type(exc).__name__}]"
                ),
            ) from None
    if response.status_code in range(300, 400):
        raise InstallerError(
            "That address redirects somewhere else, and this installer will not follow it.",
            EXIT_BAD_COORDINATOR,
            hint=(
                "Following a redirect could hand your invitation code to a "
                "different server than the one you were told about. Ask for the "
                "address that answers directly."
            ),
        )
    if response.status_code != 200:
        raise InstallerError(
            f"{origin} answered, but not like a Mycelium coordinator "
            f"(HTTP {response.status_code}).",
            EXIT_UNREACHABLE,
            hint="Check the address with whoever invited you.",
        )
    try:
        window = response.json()
    except ValueError:
        raise InstallerError(
            f"{origin} answered with something this installer could not read.",
            EXIT_UNREACHABLE,
            hint="Check the address with whoever invited you.",
        ) from None
    if not isinstance(window, dict):
        raise InstallerError(
            f"{origin} answered with something this installer could not read.",
            EXIT_UNREACHABLE,
        )
    return window


def check_protocol_window(window: dict, declared: str) -> None:
    """Say which side is out of date, in the contributor's terms."""

    def _as_int(value: object) -> int | None:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    ours = _as_int(declared)
    minimum = _as_int(window.get("node_protocol_min"))
    maximum = _as_int(window.get("node_protocol_max"))
    server_version = str(window.get("server_version") or "unknown")

    if ours is None or minimum is None or maximum is None:
        raise InstallerError(
            "The coordinator did not say which worker versions it accepts.",
            EXIT_PROTOCOL_MISMATCH,
            hint="Ask whoever runs it to check that their coordinator is up to date.",
        )
    if ours < minimum:
        raise InstallerError(
            f"Your copy of Mycelium is too old for this coordinator "
            f"(it speaks version {ours}; that server needs {minimum}–{maximum}).",
            EXIT_PROTOCOL_MISMATCH,
            hint=(
                "This one is yours to fix. Update your copy of the project "
                f"(git pull, from {PROJECT_URL}) and run this again."
            ),
        )
    if ours > maximum:
        raise InstallerError(
            f"This coordinator is running older software than your copy "
            f"(you speak version {ours}; it accepts {minimum}–{maximum}, "
            f"server version {server_version}).",
            EXIT_PROTOCOL_MISMATCH,
            hint=(
                "This one is not yours to fix, and updating your copy again will "
                "not help. Tell whoever runs the coordinator that their server "
                "needs updating, and quote the two version numbers above."
            ),
        )


async def enrol(
    origin: str,
    node_id: str,
    identity_path: Path,
    invitation: str,
    model: str,
) -> None:
    """Create the identity file, then register once, then remember the enrolment."""

    _step(8, "Introducing this computer to the coordinator")

    # Imported here rather than at module scope so the descriptor build, the
    # registration request, and its secret redaction are all the worker's own
    # code — a second implementation of credential handling would itself be the
    # regression this whole file is trying to avoid.
    from node import _apply_registration, build_stock_capability_descriptor, register

    config = get_config()
    try:
        descriptor = await build_stock_capability_descriptor(
            model=model,
            context_tokens=config.get("context_tokens"),
            ollama_url=str(config.get("ollama_url") or "http://localhost:11434"),
            config_overrides=config.get("worker_capability_overrides"),
            override_file=None,
        )
    except Exception as exc:
        raise InstallerError(
            "Could not describe this computer to the coordinator.",
            EXIT_REGISTRATION,
            hint=f"[technical detail: {type(exc).__name__}]",
        ) from None

    window = await fetch_protocol_window(origin)
    check_protocol_window(window, descriptor.executor.worker_protocol_version)
    _ok(
        "version check passed "
        f"(worker {descriptor.executor.worker_protocol_version}, coordinator accepts "
        f"{window.get('node_protocol_min')}–{window.get('node_protocol_max')})"
    )

    try:
        identity = create_worker_identity(
            identity_path, coordinator=origin, node_id=node_id
        )
    except WorkerIdentityError as exc:
        raise InstallerError(
            f"Could not create the identity file: {exc}",
            EXIT_IDENTITY,
            hint=(
                "If you have joined this coordinator from this machine before, "
                "the file already exists and nothing needs to be done. To start "
                "over, run 'python worker_installer.py uninstall' first."
            ),
        ) from None
    _ok(f"private identity written to {identity_path}")

    try:
        registration = await register(
            origin,
            node_id,
            secret=invitation,
            capability_descriptor=descriptor,
            model=model,
            enrollment_action="bootstrap",
            enrollment_credential=identity.enrollment_credential,
        )
    except Exception as exc:
        _remove_identity_file(identity_path)
        message = _safe_message(exc, invitation, identity.enrollment_credential)
        raise InstallerError(
            f"The coordinator did not accept this computer: {message}",
            EXIT_REGISTRATION,
            hint=(
                "The most common cause is a mistyped invitation code. Nothing was "
                "left behind — the identity file just created has been removed, so "
                "running this again starts cleanly."
            ),
        ) from None

    session: dict = {}
    try:
        _apply_registration(
            session,
            registration,
            identity=identity,
            identity_file=identity_path,
            expected_descriptor=descriptor,
        )
    except Exception as exc:
        _remove_identity_file(identity_path)
        message = _safe_message(exc, invitation, identity.enrollment_credential)
        raise InstallerError(
            f"The coordinator's answer did not match this computer: {message}",
            EXIT_REGISTRATION,
            hint=(
                "Nothing was left behind. This usually means the coordinator is "
                "running different software than your copy."
            ),
        ) from None
    _ok("registered, and this machine now has its own revocable credential")


def _safe_message(error: BaseException, *secrets_to_hide: str) -> str:
    """One line of an error, with any secret-shaped value taken out of it.

    `node.register` already redacts what it echoes; this is the second belt, for
    an exception raised somewhere that never saw the secret at all but might
    have been handed it by a library.
    """

    text = f"{type(error).__name__}: {error}"
    for secret in sorted({value for value in secrets_to_hide if value}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text[:400]


def _remove_identity_file(identity_path: Path) -> bool:
    """Delete an identity file, refusing anything that is not a real file."""

    try:
        if identity_path.is_symlink() or not identity_path.exists():
            return False
        if not identity_path.is_file():
            return False
        identity_path.unlink()
        return True
    except OSError:
        return False


# ── Step 9: what to do next ──────────────────────────────────────────────────

def print_next_steps(origin: str, identity_path: Path) -> None:
    _step(9, "Done")
    console.print(
        Panel(
            "[bold green]This computer has joined the network.[/bold green]\n\n"
            "[bold]To start working:[/bold]\n"
            f"  [cyan]python node.py --server {origin}[/cyan]\n"
            "  It waits for tasks and shows each one as it arrives. You do not\n"
            "  need the invitation code again — this machine has its own now.\n\n"
            "[bold]To stop:[/bold]\n"
            "  Press [cyan]Ctrl+C[/cyan], or close the window. Work you were holding\n"
            "  goes back to the network and is given to somebody else.\n\n"
            "[bold]To stop taking new work but finish what you have (drain):[/bold]\n"
            "  Press [cyan]Ctrl+C[/cyan] once and let the current task finish, or run\n"
            "  [cyan]python worker_installer.py uninstall[/cyan], which drains first.\n\n"
            "[bold]To remove everything this set up:[/bold]\n"
            "  [cyan]python worker_installer.py uninstall[/cyan]\n"
            f"  It tells the coordinator you are leaving and deletes\n"
            f"  [dim]{identity_path}[/dim].\n"
            "  It will not remove Ollama or the model — those are yours, and it\n"
            "  says so rather than deciding for you.",
            title="[bold]You are in[/bold]",
            border_style="green",
        )
    )


# ── The install flow ─────────────────────────────────────────────────────────

async def install(
    *,
    coordinator: str | None = None,
    invitation_file: Path | None = None,
    identity_file: Path | None = None,
    node_id: str | None = None,
) -> int:
    check_privileges()
    check_platform()
    present = await check_ollama_present()
    origin = ask_coordinator(coordinator)

    model = _validate_model_name(str(get_config().get("model") or DEFAULT_MODEL))
    try:
        label = normalize_worker_node_id(node_id or platform.node())
        target = (
            Path(identity_file).expanduser()
            if identity_file is not None
            else default_identity_file(origin)
        )
    except WorkerIdentityError as exc:
        raise InstallerError(
            f"This computer's name cannot be used as a node name: {exc}",
            EXIT_IDENTITY,
            hint="Run again with --node-id and a simple name, such as --node-id laptop.",
        ) from None

    # Consent covers the model download as well as the identity file, so it has
    # to come before both. ROADMAP §2: consent before installation, always.
    confirm(origin, model, target)

    invitation = read_invitation(invitation_file)
    await ensure_model(model, present)
    await enrol(origin, label, target, invitation, model)
    print_next_steps(origin, target)
    return EXIT_OK


# ── Uninstall ────────────────────────────────────────────────────────────────

async def _drain(origin: str, identity_path: Path, model: str) -> str:
    """Tell the coordinator this machine is leaving. Best effort, and honest.

    Draining needs a live session, and a session only exists after registering,
    so this registers once with the credential already on disk and then drains
    that session. If the coordinator cannot be reached, that is reported rather
    than hidden — and the credential is removed either way, because the
    contributor asked to leave.
    """

    from node import build_stock_capability_descriptor, register

    try:
        identity = load_worker_identity(identity_path, coordinator=origin)
    except WorkerIdentityError as exc:
        return f"could not read the identity file ({exc}); nothing was sent"

    config = get_config()
    try:
        descriptor = await build_stock_capability_descriptor(
            model=model,
            context_tokens=config.get("context_tokens"),
            ollama_url=str(config.get("ollama_url") or "http://localhost:11434"),
            config_overrides=config.get("worker_capability_overrides"),
            override_file=None,
        )
        registration = await register(
            origin,
            identity.node_id,
            capability_descriptor=descriptor,
            model=model,
            enrollment_action="returning",
            enrollment_credential=identity.enrollment_credential,
        )
    except Exception as exc:
        return (
            "could not reach the coordinator "
            f"({_safe_message(exc, identity.enrollment_credential)}); "
            "it will notice this machine is gone within about 90 seconds"
        )

    token = str(registration.get("session_token") or "")
    if not token:
        return "the coordinator did not issue a session, so there was nothing to drain"
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.post(
                f"{origin}/nodes/{identity.node_id}/drain",
                headers={"X-Node-Session": token},
            )
    except httpx.HTTPError as exc:
        return f"the drain request did not arrive ({type(exc).__name__})"
    if response.status_code != 200:
        return f"the coordinator refused the drain request (HTTP {response.status_code})"
    return "drained cleanly; the coordinator has stopped sending work"


async def uninstall(
    *,
    coordinator: str | None = None,
    identity_file: Path | None = None,
    assume_yes: bool = False,
) -> int:
    console.print("\n[bold cyan]Leaving the network[/bold cyan]")
    check_privileges()

    if identity_file is not None:
        target = Path(identity_file).expanduser()
        origin = coordinator
        if origin is not None:
            origin = ask_origin_quietly(origin)
    else:
        origin = ask_origin_quietly(
            coordinator
            or _read_line("  Which address are you leaving? (https://…): ")
        )
        target = default_identity_file(origin)

    if not target.exists():
        console.print(
            f"  [yellow]No identity file at[/yellow] [dim]{target}[/dim]\n"
            "  Nothing to remove — this machine is not enrolled there."
        )
        return EXIT_OK

    if origin is None:
        try:
            origin = load_worker_identity(target).coordinator
        except WorkerIdentityError:
            origin = None

    console.print(f"  This will delete [dim]{target}[/dim]")
    if not assume_yes:
        answer = _read_line("  Type 'yes' to remove it, or press Enter to keep it: ").lower()
        if answer not in {"y", "yes"}:
            console.print("  [dim]Kept. Nothing was changed.[/dim]")
            return EXIT_DECLINED

    if origin:
        model = _validate_model_name(str(get_config().get("model") or DEFAULT_MODEL))
        console.print("  Telling the coordinator you are leaving…")
        console.print(f"  [dim]{await _drain(origin, target, model)}[/dim]")

    removed = _remove_identity_file(target)
    console.print(
        Panel(
            ("[bold]Removed:[/bold]\n"
             f"  - your private credential file, [dim]{target}[/dim]\n"
             if removed else
             "[bold]Removed:[/bold]\n  - nothing; the credential file could not be deleted\n")
            + "\n[bold]Deliberately left alone — these are yours, not ours:[/bold]\n"
            "  - Ollama itself. Remove it the way you installed it.\n"
            f"  - the downloaded model (about 2.5 GB). To remove it: "
            f"[cyan]ollama rm {get_config().get('model') or DEFAULT_MODEL}[/cyan]\n"
            "  - this folder, the project's code, and Python.\n"
            "  - any output files under [dim]output/[/dim].\n\n"
            "Nothing about your firewall, certificates, or startup programs was "
            "ever changed, so there is nothing to undo there.",
            title="[bold]Uninstalled[/bold]",
            border_style="cyan",
        )
    )
    return EXIT_OK if removed else EXIT_IDENTITY


def ask_origin_quietly(raw: str) -> str:
    try:
        return normalize_coordinator(raw)
    except WorkerIdentityError as exc:
        raise InstallerError(
            f"That address cannot be used: {exc}",
            EXIT_BAD_COORDINATOR,
        ) from None


# ── Entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worker_installer.py",
        description="Join a Mycelium coordinator as a worker, or leave one.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="install",
        choices=("install", "uninstall"),
        help="install (the default) walks through joining; uninstall drains and removes",
    )
    parser.add_argument(
        "--coordinator",
        default=None,
        help="The coordinator address, if you would rather not be asked for it",
    )
    parser.add_argument(
        "--invitation-file",
        type=Path,
        default=None,
        help=(
            "Read the invitation code from this file instead of typing it. The "
            "file must be readable only by you. This takes a path, never the "
            "code itself — a code on the command line is visible to every "
            "program on the machine and is written to your shell history"
        ),
    )
    parser.add_argument(
        "--identity-file",
        type=Path,
        default=None,
        help="Where to keep this machine's private credential (default: your config directory)",
    )
    parser.add_argument(
        "--node-id",
        default=None,
        help="A name for this machine on the network (default: its hostname)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="For uninstall only: do not ask again before deleting the credential",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "uninstall":
            return asyncio.run(
                uninstall(
                    coordinator=args.coordinator,
                    identity_file=args.identity_file,
                    assume_yes=args.yes,
                )
            )
        return asyncio.run(
            install(
                coordinator=args.coordinator,
                invitation_file=args.invitation_file,
                identity_file=args.identity_file,
                node_id=args.node_id,
            )
        )
    except InstallerError as exc:
        # The whole error surface. A traceback here would be both useless to the
        # person reading it and a way for a value to escape into the terminal.
        console.print(f"\n[red bold]Stopped:[/red bold] {exc}")
        if exc.hint:
            console.print(f"\n{exc.hint}")
        console.print(f"\n[dim]exit code {exc.exit_code}[/dim]")
        return exc.exit_code
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled. Nothing was left half-done.[/dim]")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - a traceback must never be the error surface
        console.print(
            f"\n[red bold]Stopped:[/red bold] something went wrong that this "
            f"installer did not expect.\n[dim]{type(exc).__name__}[/dim]"
        )
        console.print(
            "\nNothing was left running. If this keeps happening, report it at "
            f"{PROJECT_URL}/issues and include the line above."
        )
        return EXIT_REGISTRATION


if __name__ == "__main__":
    sys.exit(run())
