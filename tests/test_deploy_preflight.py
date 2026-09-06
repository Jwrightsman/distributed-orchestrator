"""The host preflight must be right about the host, and silent about secrets.

Two properties carry most of the weight here. It has to *find* the states that
would expose a credential -- an application port on a public interface, SSH
password login, a world-readable config -- because a preflight that passes a
broken host is worse than none. And it must never print a credential value,
because an operator will paste this output into a chat window to ask for help.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.deploy_preflight import (  # noqa: E402
    FAIL,
    PASS,
    SKIP,
    WARN,
    Finding,
    Report,
    _decode_proc_address,
    check_file_modes,
    check_secrets,
    check_ssh,
    check_unattended_upgrades,
    classify_secret,
    is_public_bind,
    render,
    run_host_preflight,
    shannon_entropy_bits,
)


def _by_name(findings, name):
    matches = [finding for finding in findings if finding.name == name]
    assert matches, f"no finding named {name} in {[f.name for f in findings]}"
    return matches[0]


# -- Reading the kernel's socket table ----------------------------------------


@pytest.mark.parametrize(
    "raw, ipv6, expected",
    [
        ("0100007F:1F40", False, ("127.0.0.1", 8000)),  # 0x1F40 == 8000
        ("00000000:0016", False, ("0.0.0.0", 22)),
        ("0100007F:01BB", False, ("127.0.0.1", 443)),
        (
            "00000000000000000000000001000000:1F40",
            True,
            ("::1", 8000),
        ),
        (
            "00000000000000000000000000000000:0050",
            True,
            ("::", 80),
        ),
    ],
)
def test_proc_addresses_decode(raw, ipv6, expected):
    assert _decode_proc_address(raw, ipv6=ipv6) == expected


@pytest.mark.parametrize(
    "host, public",
    [
        ("0.0.0.0", True),
        ("::", True),
        ("203.0.113.10", True),
        ("100.101.102.103", True),  # a tailnet address is another machine's route
        ("192.168.1.50", True),
        ("127.0.0.1", False),
        ("127.5.5.5", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_public_bind_classification(host, public):
    assert is_public_bind(host) is public


# -- Secrets ------------------------------------------------------------------


def test_a_generated_credential_is_strong():
    status, entropy_class, bits = classify_secret(secrets.token_urlsafe(32))

    assert status == PASS
    assert entropy_class == "strong"
    assert bits >= 128


@pytest.mark.parametrize(
    "value, expected_class",
    [
        ("", "empty or unset"),
        (None, "empty or unset"),
        ("short", "shorter than the 32-character minimum"),
        ("a" * 40, "very low entropy"),
        ("abababababababababababababababababab", "very low entropy"),
        ("<independent-random-node-authority-at-least-32-chars>", "a placeholder from the documentation"),
        ("changeme-changeme-changeme-changeme", "a placeholder from the documentation"),
        ("correct horse battery staple and then some more", "typed rather than generated"),
        ("the-quick-brown-fox-jumps-over-the-lazy-dog", "typed rather than generated"),
    ],
)
def test_weak_admission_secrets_fail_with_a_reason(value, expected_class):
    status, entropy_class, _bits = classify_secret(value)

    assert status == FAIL
    assert entropy_class == expected_class


def test_a_typed_passphrase_is_refused_despite_scoring_well_on_entropy():
    """Character-frequency entropy rates prose higher than a random token.

    This is the one place the estimator is wrong in the dangerous direction,
    so the shape check has to run in front of it. Without that, an operator
    who invented a memorable phrase would be told it was strong.
    """

    phrase = "correct horse battery staple and then some more words"
    status, entropy_class, bits = classify_secret(phrase)

    assert bits > 128, "the estimator really does over-rate this"
    assert status == FAIL
    assert entropy_class == "typed rather than generated"


def test_the_bit_count_is_not_quoted_for_a_typed_phrase(tmp_path):
    """Quoting a high number next to a refusal would reassure the wrong reader."""

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"node_secret": "correct horse battery staple and then more"}),
        encoding="utf-8",
    )

    summary = _by_name(check_secrets(config), "secret_node_secret").summary

    assert "typed rather than generated" in summary
    assert "bits" not in summary


def test_the_32_character_minimum_is_not_enough_on_its_own():
    """Length was the only rule config.py enforces; entropy is the real one."""

    status, _class, _bits = classify_secret("abcd" * 8)  # exactly 32 characters

    assert status == FAIL


def test_entropy_estimate_is_conservative_about_generated_values():
    """It should under-report rather than over-report a real token."""

    bits = shannon_entropy_bits(secrets.token_urlsafe(32))

    assert 128 <= bits <= 256


def test_reused_authorities_are_reported(tmp_path):
    shared = secrets.token_urlsafe(32)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"node_secret": shared, "pitch_key": shared, "viewer_key": secrets.token_urlsafe(32)}
        ),
        encoding="utf-8",
    )

    finding = _by_name(check_secrets(config), "authority_separation")

    assert finding.status == FAIL


def test_distinct_strong_authorities_pass(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "node_secret": secrets.token_urlsafe(32),
                "pitch_key": secrets.token_urlsafe(32),
                "viewer_key": secrets.token_urlsafe(32),
            }
        ),
        encoding="utf-8",
    )

    findings = check_secrets(config)

    assert all(finding.status == PASS for finding in findings)


def test_a_broken_config_file_fails_rather_than_passing_quietly(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")

    assert _by_name(check_secrets(config), "admission_secret").status == FAIL


# -- No secret ever reaches the output ----------------------------------------


def test_no_secret_value_appears_anywhere_in_the_report(tmp_path):
    values = {
        "node_secret": secrets.token_urlsafe(32),
        "pitch_key": secrets.token_urlsafe(32),
        "viewer_key": secrets.token_urlsafe(32),
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values), encoding="utf-8")

    report = run_host_preflight(state_dir=tmp_path, config_path=config)
    rendered = render(report)
    as_json = report.as_json()

    for name, value in values.items():
        assert value not in rendered, f"{name} leaked into the human report"
        assert value not in as_json, f"{name} leaked into the JSON report"
        # Not even a recognisable prefix.
        assert value[:12] not in rendered
        assert value[:12] not in as_json


def test_a_weak_secret_is_reported_without_showing_it(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"node_secret": "hunter2hunter2hunter2hunter2hunter2"}), "utf-8")

    report = run_host_preflight(state_dir=tmp_path, config_path=config)

    assert report.ok is False
    assert "hunter2" not in render(report)


# -- SSH ----------------------------------------------------------------------


def _sshd(tmp_path: Path, body: str) -> tuple[Path, Path]:
    config = tmp_path / "sshd_config"
    config.write_text(body, encoding="utf-8")
    directory = tmp_path / "sshd_config.d"
    directory.mkdir()
    return config, directory


def test_password_authentication_on_is_a_failure(tmp_path):
    config, directory = _sshd(tmp_path, "PasswordAuthentication yes\n")

    finding = _by_name(check_ssh(config, directory), "ssh_password_authentication")

    assert finding.status == FAIL
    assert "sudo systemctl reload ssh" in finding.fix


def test_password_authentication_defaults_to_on_when_unset(tmp_path):
    """sshd's own default is yes, so silence is not safety."""

    config, directory = _sshd(tmp_path, "# nothing set here\n")

    assert _by_name(check_ssh(config, directory), "ssh_password_authentication").status == FAIL


def test_a_drop_in_file_overrides_the_main_config(tmp_path):
    config, directory = _sshd(tmp_path, "PasswordAuthentication yes\n")
    (directory / "99-mycelium.conf").write_text(
        "PasswordAuthentication no\nPermitRootLogin no\n", encoding="utf-8"
    )

    findings = check_ssh(config, directory)

    assert _by_name(findings, "ssh_password_authentication").status == PASS
    assert _by_name(findings, "ssh_root_login").status == PASS


def test_root_login_with_a_password_is_a_failure(tmp_path):
    config, directory = _sshd(tmp_path, "PermitRootLogin yes\n")

    assert _by_name(check_ssh(config, directory), "ssh_root_login").status == FAIL


def test_key_only_root_login_is_a_warning_not_a_failure(tmp_path):
    config, directory = _sshd(tmp_path, "PermitRootLogin prohibit-password\n")

    assert _by_name(check_ssh(config, directory), "ssh_root_login").status == WARN


def test_no_ssh_configuration_is_skipped_rather_than_assumed_safe(tmp_path):
    missing = tmp_path / "nothing"

    assert _by_name(check_ssh(missing, missing), "ssh_config").status == SKIP


# -- Unattended upgrades ------------------------------------------------------


def test_unattended_upgrades_enabled(tmp_path):
    apt = tmp_path / "apt"
    apt.mkdir()
    auto = apt / "20auto-upgrades"
    auto.write_text(
        'APT::Periodic::Update-Package-Lists "1";\n'
        'APT::Periodic::Unattended-Upgrade "1";\n',
        encoding="utf-8",
    )

    assert check_unattended_upgrades(apt, auto)[0].status == PASS


def test_unattended_upgrades_switched_off_is_a_warning(tmp_path):
    apt = tmp_path / "apt"
    apt.mkdir()
    auto = apt / "20auto-upgrades"
    auto.write_text('APT::Periodic::Unattended-Upgrade "0";\n', encoding="utf-8")

    finding = check_unattended_upgrades(apt, auto)[0]

    assert finding.status == WARN
    assert "unattended-upgrades" in finding.fix


# -- File modes ---------------------------------------------------------------


def _state(tmp_path: Path, directory_mode: int, file_mode: int) -> tuple[Path, Path]:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    config.chmod(file_mode)
    database = tmp_path / "events.db"
    database.write_bytes(b"")
    database.chmod(file_mode)
    tmp_path.chmod(directory_mode)
    return config, database


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_world_readable_config_in_an_open_directory_is_a_failure(tmp_path):
    config, _database = _state(tmp_path, 0o755, 0o644)

    findings = check_file_modes(tmp_path, config)

    assert _by_name(findings, "state_directory").status == FAIL
    assert _by_name(findings, "config_file").status == FAIL


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_owner_only_everything_passes(tmp_path):
    config, _database = _state(tmp_path, 0o700, 0o600)

    assert all(
        finding.status == PASS for finding in check_file_modes(tmp_path, config)
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_closed_directory_downgrades_a_loose_file_to_a_warning(tmp_path):
    """SQLite creates events.db at 0644 and nothing here can stop it.

    Failing on that regardless of the directory would mean every correct
    deployment fails this check forever, which is how a preflight teaches
    people to ignore it. A 0700 directory denies traversal, so the file is
    genuinely unreachable and the finding is advice rather than an alarm.
    """

    config, database = _state(tmp_path, 0o700, 0o644)

    findings = check_file_modes(tmp_path, config)

    assert _by_name(findings, "state_directory").status == PASS
    assert _by_name(findings, "config_file").status == WARN
    assert _by_name(findings, "events_database").status == WARN
    assert _by_name(findings, "events_database").fix == f"chmod 600 {database}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_group_readable_state_directory_is_a_failure(tmp_path):
    config, _database = _state(tmp_path, 0o750, 0o600)

    finding = _by_name(check_file_modes(tmp_path, config), "state_directory")

    assert finding.status == FAIL
    assert finding.fix == f"chmod 700 {tmp_path}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_the_deployment_default_of_0755_does_not_pass(tmp_path):
    """`mkdir data` under a normal umask produces 0755, so deploy.sh chmods it."""

    config, _database = _state(tmp_path, 0o755, 0o600)

    assert _by_name(check_file_modes(tmp_path, config), "state_directory").status == FAIL


@pytest.mark.skipif(os.name == "posix", reason="the non-POSIX branch")
def test_file_modes_are_skipped_where_they_mean_nothing(tmp_path):
    assert check_file_modes(tmp_path, tmp_path / "config.json")[0].status == SKIP


# -- Report shape -------------------------------------------------------------


def test_every_failure_names_a_fix():
    report = Report(
        ok=False,
        findings=(
            Finding("a", FAIL, "broken", why="because", fix="do the thing"),
            Finding("b", PASS, "fine", why="reasons"),
        ),
    )
    rendered = render(report)

    assert "do the thing" in rendered
    assert "Do not invite anybody" in rendered


def test_failures_sort_above_passes():
    report = Report(
        ok=False,
        findings=(
            Finding("fine", PASS, "ok"),
            Finding("broken", FAIL, "not ok"),
        ),
    )
    rendered = render(report)

    assert rendered.index("broken") < rendered.index("fine")


def test_the_report_says_it_changed_nothing():
    rendered = render(Report(ok=True, findings=()))

    assert "Read-only" in rendered


def test_json_output_is_valid_and_carries_every_finding(tmp_path):
    report = run_host_preflight(state_dir=tmp_path)
    payload = json.loads(report.as_json())

    assert payload["ok"] is True
    assert len(payload["findings"]) == len(report.findings)
    assert all("why" in finding for finding in payload["findings"])


def test_no_url_skips_the_network_checks_rather_than_inventing_them(tmp_path):
    report = run_host_preflight(state_dir=tmp_path)

    assert _by_name(report.findings, "certificate").status == SKIP
    assert not any(
        finding.name == "worker_protocol_window" for finding in report.findings
    )
