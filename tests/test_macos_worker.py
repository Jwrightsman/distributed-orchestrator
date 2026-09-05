"""macOS, which until now was a code path with no test behind it.

`worker_identity` has always aimed the identity file at
`~/Library/Application Support/Mycelium/`, and nothing had ever checked that it
lands there, that it lands with the right permissions, or that a path with a
space in it — which every Mac has, in that exact directory name — survives the
round trip.

Two kinds of test live here, and the difference matters more than usual:

* **Injected.** Every function that decides something about macOS takes the
  platform as an argument, so the decision can be exercised anywhere. These run
  on Linux, on Windows, and on a Mac, and prove the *logic*.
* **Executed.** Marked `darwin_only` or `posix_only`. These prove the *machine*,
  and they only prove it where they actually run. On Windows they are skipped
  and the property is unproven, not proven — which is exactly why CI grew a
  macOS job in the same change that added this file.

The Ollama gap is the one worth reading first. On macOS Ollama arrives as a disk
image containing an application, and the `ollama` command-line tool is a link
that application creates the first time somebody opens it. So the normal state
of a Mac ten seconds after installing Ollama is: installed, not running, no
command. Telling that person to install Ollama is telling them to redo the thing
they just did, and it reads as the installer being broken.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path

import pytest

import node
import worker_identity
import worker_installer
from worker_identity import (
    create_worker_identity,
    default_identity_file,
    load_worker_identity,
    user_config_directory,
)


darwin_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="executed macOS behaviour; reasoned elsewhere"
)
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX permission bits; skipped on Windows"
)


# ── Ollama: installed, running, and on the path are three different things ───


def test_a_mac_with_ollama_installed_but_never_opened_is_told_to_open_it():
    """THE ONE THAT MATTERS. The most common state of a fresh Mac."""

    hint = worker_installer.ollama_absence_hint(system="Darwin", app_present=True)

    assert "already installed" in hint, "must not tell them to install it again"
    assert "Applications" in hint, "must say where to open it from"
    assert "menu bar" in hint, "must say how they can tell it worked"
    assert worker_installer.OLLAMA_INSTALL_URL not in hint, (
        "sending somebody back to the download page for software they already "
        "have is what made this read as broken"
    )


def test_a_mac_without_ollama_is_told_where_to_get_it():
    hint = worker_installer.ollama_absence_hint(system="Darwin", app_present=False)
    assert worker_installer.OLLAMA_INSTALL_URL in hint
    assert "open it once" in hint, "the Mac-specific second step is the trap"
    assert "will not install it for you" in hint


def test_linux_still_gets_the_linux_answer():
    hint = worker_installer.ollama_absence_hint(system="Linux", app_present=False)
    assert "ollama serve" in hint
    assert "menu bar" not in hint


def test_windows_still_gets_the_generic_answer():
    hint = worker_installer.ollama_absence_hint(system="Windows", app_present=False)
    assert worker_installer.OLLAMA_INSTALL_URL in hint
    assert "Applications folder" not in hint


def test_the_missing_command_hint_does_not_ask_them_to_edit_anything():
    """Fixing somebody's search path for them is out of scope, permanently."""

    hint = worker_installer.ollama_cli_missing_hint(system="Darwin")
    assert "Close this terminal window and open a new one" in hint
    assert "will not change your search path" in hint
    for forbidden in ("export PATH", ".zshrc", ".bash_profile", "/usr/local/bin"):
        assert forbidden not in hint, (
            f"the hint tells the contributor to edit {forbidden}; the fix is to "
            "reopen the terminal, and this program does not edit shell settings"
        )


def test_the_application_is_only_looked_for_on_a_mac(tmp_path):
    """A directory called Ollama.app on Linux means nothing."""

    pretend = tmp_path / "Ollama.app"
    pretend.mkdir()
    assert worker_installer.macos_ollama_app_present(system="Darwin", app_path=pretend)
    assert not worker_installer.macos_ollama_app_present(system="Linux", app_path=pretend)
    assert not worker_installer.macos_ollama_app_present(
        system="Darwin", app_path=tmp_path / "absent.app"
    )


def test_the_installer_looks_in_the_place_the_disk_image_installs_to():
    assert worker_installer.MACOS_OLLAMA_APP == Path("/Applications/Ollama.app")


@pytest.mark.asyncio
async def test_the_daemon_is_what_counts_not_whether_the_command_exists(monkeypatch):
    """The service does the work. The command is a convenience for pulling."""

    async def answering():
        return {"ok": True, "models": ["qwen3.5:4b"]}

    monkeypatch.setattr(worker_installer, "check_ollama", answering)
    monkeypatch.setattr(worker_installer.shutil, "which", lambda _name: None)

    assert await worker_installer.check_ollama_present() == ["qwen3.5:4b"]


@pytest.mark.asyncio
async def test_a_missing_command_is_mentioned_but_does_not_stop_the_join(
    monkeypatch, capsys
):
    async def answering():
        return {"ok": True, "models": ["qwen3.5:4b"]}

    monkeypatch.setattr(worker_installer, "check_ollama", answering)
    monkeypatch.setattr(worker_installer.shutil, "which", lambda _name: None)
    await worker_installer.check_ollama_present()

    said = " ".join(capsys.readouterr().out.split())
    assert "not on this terminal's path" in said
    assert "only matters if the model still has to be downloaded" in said


@pytest.mark.asyncio
async def test_an_unreachable_ollama_carries_the_platform_specific_hint(monkeypatch):
    async def absent():
        return {"ok": False, "models": [], "error": "not running"}

    monkeypatch.setattr(worker_installer, "check_ollama", absent)
    monkeypatch.setattr(worker_installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        worker_installer, "macos_ollama_app_present", lambda **_k: True
    )

    with pytest.raises(worker_installer.InstallerError) as raised:
        await worker_installer.check_ollama_present()
    assert raised.value.exit_code == worker_installer.EXIT_OLLAMA_MISSING
    assert "already installed" in raised.value.hint


@darwin_only
def test_on_this_mac_the_two_questions_are_asked_independently():
    """Executed: whatever this machine's state is, both probes answer."""

    assert isinstance(worker_installer.ollama_cli_available(), bool)
    assert isinstance(worker_installer.macos_ollama_app_present(), bool)


# ── Apple Silicon versus Intel ───────────────────────────────────────────────


@pytest.mark.parametrize("machine", ["arm64", "aarch64"])
def test_apple_silicon_is_named_the_way_its_owner_names_it(machine):
    described = worker_installer.describe_machine("Darwin", machine)
    assert "Apple Silicon" in described
    assert machine in described
    assert "GPU" in described


@pytest.mark.parametrize("machine", ["x86_64", "amd64"])
def test_an_intel_mac_is_told_it_is_running_on_the_processor(machine):
    described = worker_installer.describe_machine("Darwin", machine)
    assert "Intel" in described
    assert "processor" in described


def test_other_systems_are_reported_without_being_reinterpreted():
    assert worker_installer.describe_machine("Linux", "x86_64") == "x86_64"
    assert worker_installer.describe_machine("Windows", "AMD64") == "AMD64"


def test_the_platform_step_reports_the_processor(capsys):
    worker_installer.check_platform(system="Darwin", version=(3, 14), machine="arm64")
    said = " ".join(capsys.readouterr().out.split())
    assert "Apple Silicon (arm64)" in said


def test_apple_silicon_reaches_the_capability_descriptor(monkeypatch):
    """The one hardware fact the coordinator is told, and it must be right."""

    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")
    hardware = node._detected_hardware_descriptor()
    assert hardware.architecture == "arm64"


def test_the_descriptor_lower_cases_what_the_machine_reports(monkeypatch):
    monkeypatch.setattr(node.platform, "machine", lambda: "ARM64")
    assert node._detected_hardware_descriptor().architecture == "arm64"


@darwin_only
def test_this_mac_reports_a_real_architecture_into_the_descriptor():
    """Executed: no monkeypatch, whatever this runner actually is."""

    hardware = node._detected_hardware_descriptor()
    assert hardware.architecture in {"arm64", "x86_64"}
    assert hardware.total_memory_bytes is not None, (
        "the macOS sysctl fallback did not detect physical memory"
    )


# ── Where the identity file goes, and what it is allowed to be ───────────────


def test_the_config_directory_is_the_one_macos_reserves_for_this(tmp_path):
    directory = user_config_directory(
        environ={}, home=tmp_path / "Users" / "someone", platform_name="darwin", os_name="posix"
    )
    assert directory == tmp_path / "Users" / "someone" / "Library" / "Application Support" / "Mycelium"


def test_linux_and_windows_are_unaffected_by_the_macos_branch(tmp_path):
    assert user_config_directory(
        environ={}, home=tmp_path, platform_name="linux", os_name="posix"
    ) == tmp_path / ".config" / "mycelium"
    assert user_config_directory(
        environ={"APPDATA": str(tmp_path / "Roaming")}, home=tmp_path, os_name="nt"
    ) == tmp_path / "Roaming" / "Mycelium"


@darwin_only
def test_on_this_mac_the_default_lands_under_application_support():
    """Executed: the real HOME, the real platform, no arguments."""

    identity = default_identity_file("https://coordinator.example")
    parts = identity.parts
    assert "Library" in parts and "Application Support" in parts and "Mycelium" in parts
    assert identity.parent.name == "nodes"
    assert identity.suffix == ".json"


def test_a_path_containing_spaces_survives_the_round_trip(tmp_path):
    """"Application Support" has a space in it. Every Mac. Every time."""

    root = tmp_path / "Library" / "Application Support" / "Mycelium"
    target = default_identity_file("https://coordinator.example", config_dir=root)
    assert " " in str(target)

    created = create_worker_identity(
        target, coordinator="https://coordinator.example", node_id="laptop"
    )
    reloaded = load_worker_identity(target, coordinator="https://coordinator.example")
    assert reloaded.enrollment_credential == created.enrollment_credential
    assert target.exists()


@posix_only
def test_the_identity_file_is_owner_only_and_so_is_its_directory(tmp_path):
    """Executed on Linux and macOS CI, skipped on Windows, never assumed."""

    root = tmp_path / "Library" / "Application Support" / "Mycelium"
    target = default_identity_file("https://coordinator.example", config_dir=root)
    create_worker_identity(
        target, coordinator="https://coordinator.example", node_id="laptop"
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) & 0o077 == 0, (
        "the nodes/ directory is readable or writable by somebody else"
    )


@posix_only
def test_an_identity_file_somebody_else_can_read_is_refused(tmp_path):
    root = tmp_path / "Library" / "Application Support" / "Mycelium"
    target = default_identity_file("https://coordinator.example", config_dir=root)
    create_worker_identity(
        target, coordinator="https://coordinator.example", node_id="laptop"
    )
    target.chmod(0o644)

    with pytest.raises(worker_identity.WorkerIdentityError) as raised:
        load_worker_identity(target, coordinator="https://coordinator.example")
    assert "0600" in str(raised.value)


# ── Running as root, on a Mac, is refused exactly as it is on Linux ──────────


def test_root_is_refused_through_geteuid_wherever_geteuid_exists(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert worker_installer.running_privileged() is True

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    assert worker_installer.running_privileged() is False


def test_the_refusal_names_sudo_because_that_is_how_it_happens(monkeypatch):
    monkeypatch.setattr(worker_installer, "running_privileged", lambda: True)
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer.check_privileges()
    assert raised.value.exit_code == worker_installer.EXIT_PRIVILEGED
    assert "sudo" in raised.value.hint


@posix_only
def test_this_run_is_not_root_and_the_check_agrees():
    """Executed: the probe reads the real uid, not a monkeypatched one."""

    assert (os.geteuid() == 0) == worker_installer.running_privileged()


def test_darwin_is_a_supported_platform():
    assert "Darwin" in worker_installer.SUPPORTED_SYSTEMS
    worker_installer.check_platform(system="Darwin", version=(3, 14), machine="arm64")


# ── Gatekeeper: what this program is not allowed to touch ────────────────────


#: Commands and files that change what macOS will let a machine do, or what its
#: owner's shell will find. Reading any of them is fine; none of these is a read.
_SECURITY_POSTURE_COMMANDS = (
    "xattr",
    "spctl",
    "codesign",
    "com.apple.quarantine",
    "launchd",
    "launchctl",
    "defaults write",
    ".zshrc",
    ".zprofile",
    ".bashrc",
    ".bash_profile",
    "/etc/paths",
)

#: Ways to *write* to the environment. Reading it is legitimate and
#: `worker_identity` does — `XDG_CONFIG_HOME` and `APPDATA` are how a config
#: directory is found. Writing to it is not: every child process inherits it,
#: which is how `install.ps1`'s `$env:SWARM_SECRET` reached an argument list.
_ENVIRONMENT_WRITES = (
    "putenv",
    "os.environ[",
    "environ.update",
    "environ.setdefault",
)


@pytest.mark.parametrize(
    "module", [worker_installer, node, worker_identity, __import__("join")]
)
def test_no_worker_module_alters_the_hosts_security_posture(module):
    """Constraint 10: this program observes the host, it does not adjust it.

    Stripping a quarantine attribute, re-signing something, or writing a search
    path into a shell profile would each make a contributor's machine slightly
    less protected than they left it, in a way they did not agree to and would
    not notice. None of them is ever the right answer here.
    """

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    offenders = [word for word in _SECURITY_POSTURE_COMMANDS if word in source]
    assert not offenders, (
        f"{module.__name__} references {offenders}; a worker must not change "
        "the machine's security posture or its owner's shell configuration"
    )


@pytest.mark.parametrize(
    "module", [worker_installer, node, worker_identity, __import__("join")]
)
def test_no_worker_module_writes_to_the_environment(module):
    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    offenders = [word for word in _ENVIRONMENT_WRITES if word in source]
    assert not offenders, (
        f"{module.__name__} writes to the environment ({offenders}); every child "
        "process it starts would inherit whatever was put there"
    )


def test_the_installer_starts_exactly_one_program_and_it_is_the_model_pull():
    """A second subprocess is where a security-posture change would arrive."""

    tree = ast.parse(Path(worker_installer.__file__).read_text(encoding="utf-8"))
    spawned = [
        node_
        for node_ in ast.walk(tree)
        if isinstance(node_, ast.Call)
        and isinstance(node_.func, ast.Attribute)
        and node_.func.attr in {"run", "Popen", "call", "check_output", "check_call"}
        and isinstance(node_.func.value, ast.Name)
        and node_.func.value.id in {"subprocess", "os"}
    ]
    assert len(spawned) == 1
    command = spawned[0].args[0]
    assert isinstance(command, ast.List)
    assert isinstance(command.elts[0], ast.Constant)
    assert command.elts[0].value == "ollama"


def _quarantine_attribute(path: Path) -> bool | None:
    """Is `com.apple.quarantine` set on this file? None when unanswerable.

    A read, and only a read: `getxattr(2)` through libc, which cannot alter
    anything. `os.listxattr` and friends are Linux-only in the standard
    library, and shelling out to `/usr/bin/xattr` would be both less reliable
    and a subprocess in a test whose subject is not running subprocesses.
    """

    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        library = ctypes.util.find_library("c")
        if library is None:
            return None
        libc = ctypes.CDLL(library, use_errno=True)
        getxattr = libc.getxattr
        getxattr.restype = ctypes.c_ssize_t
        getxattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        result = getxattr(
            os.fsencode(str(path)), b"com.apple.quarantine", None, 0, 0, 0
        )
    except (OSError, AttributeError, ValueError):
        return None
    return result >= 0


@darwin_only
def test_a_checkout_of_this_repository_is_not_quarantined():
    """Executed: git writes files itself, so nothing marks them as downloaded.

    A zip pulled from github.com in a browser is a different story — the
    browser marks it, and unarchiving propagates the mark to what comes out.
    That is documented for contributors in docs/JOIN.md rather than worked
    around here, because working around it would mean removing a mark macOS
    put there on purpose.
    """

    for path in (Path(worker_installer.__file__), Path(node.__file__)):
        quarantined = _quarantine_attribute(path)
        if quarantined is None:
            pytest.skip("the quarantine attribute could not be read on this machine")
        assert quarantined is False, (
            f"{path.name} carries com.apple.quarantine. A git clone should not; "
            "this checkout probably came from a downloaded archive."
        )
