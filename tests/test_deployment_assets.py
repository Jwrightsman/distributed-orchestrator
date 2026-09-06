import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_uses_three_authority_migration_and_strict_health_gate():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "ensure_trusted_alpha_config" in script
    assert "viewer_key, pitch_key, and node_secret" in script
    assert "deployment_health_ready" in script
    assert "private_overlay=True" in script
    assert "MYCELIUM_PRIVATE_OVERLAY_CONFIRMED" in script
    assert "Join this host to that overlay" in script
    assert "revocable enrollment identity" in script
    assert "--mode trusted_alpha" in script
    assert "set +x" in script
    assert "NODE_SECRET=" not in script
    assert "PITCH_KEY=" not in script
    assert "openssl rand" not in script
    # SQLite writes events.db at 0644 and cannot be told otherwise, so the
    # directory mode is what keeps other local accounts out of it.
    assert "chmod 700 data" in script


def test_compose_publishes_to_loopback_and_one_persistent_state_mount():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '"127.0.0.1:8000:8000"' in compose
    assert "./data:/data" in compose
    assert "MYCELIUM_STATE_DIR: /data" in compose
    assert "scripts/node_enrollment_admin.py" in dockerfile
    assert '"--workers"' not in dockerfile


def test_the_published_port_cannot_be_moved_off_loopback_by_configuration():
    """Docker's published ports bypass ufw, so this bind is the only control.

    A published port becomes a rule in Docker's own iptables chain, which the
    kernel evaluates before the chain ufw writes into. `ufw deny 8000` then
    reports "deny" while the port keeps answering the Internet, and reading
    `ufw status` will never reveal it. Binding the host side to loopback is
    the form that a firewall cannot silently fail to apply.

    So the address is a literal. It used to be
    `${MYCELIUM_PUBLISH_ADDRESS:-127.0.0.1}`, whose default was right and
    whose override turned the trap back on from a `.env` file nobody reviews.
    Both documented deployments put a reverse proxy on the public socket
    instead, so nothing needs the variable any more.
    """

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MYCELIUM_PUBLISH_ADDRESS" not in compose, (
        "the publish address is configurable again; an operator can set it to "
        "0.0.0.0 and ufw will not tell them"
    )
    published = re.findall(r'^\s*-\s*"([^"]*:\d+:\d+)"', compose, re.MULTILINE)
    assert published, "expected at least one published port to check"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), (
            f"{mapping} is published beyond loopback"
        )


def test_the_docker_ufw_bypass_is_documented_with_a_command_that_reveals_it():
    """A trap that is only mentioned is a trap the reader does not check for."""

    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")

    assert "ufw" in deploy
    assert "iptables -L DOCKER -n" in deploy, "name the command that shows the rule"
    assert "ss -tlnp" in deploy, "name the command that shows the socket"


def test_the_caddy_configurations_ship_with_the_repository():
    """An operator should not have to write a proxy configuration from scratch."""

    for name in ("Caddyfile.tailscale", "Caddyfile.public"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "reverse_proxy 127.0.0.1:8000" in text, f"{name} must reach loopback"
        assert "max_size 12MB" in text, (
            f"{name} must sit above the coordinator's own 10 MiB result ceiling"
        )
        assert "Strict-Transport-Security" in text, f"{name} needs security headers"
        assert "read_header" in text, f"{name} needs a slowloris bound"
        # A read or write timeout would break the 25s worker long poll and the
        # dashboard WebSocket. Asserting their absence keeps a later
        # "hardening" pass from quietly breaking task handout.
        assert "\twrite_timeout" not in text and "\tread_timeout" not in text


def test_ci_docker_health_fixture_includes_durable_enrollment_readiness():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "'node_enrollment_required':True" in workflow
