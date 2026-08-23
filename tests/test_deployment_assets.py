from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_uses_three_authority_migration_and_strict_health_gate():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "ensure_trusted_alpha_config" in script
    assert "viewer_key, pitch_key, and node_secret" in script
    assert "deployment_health_ready" in script
    assert "--mode trusted_alpha" in script
    assert "set +x" in script
    assert "NODE_SECRET=" not in script
    assert "PITCH_KEY=" not in script
    assert "openssl rand" not in script


def test_compose_defaults_to_loopback_and_one_persistent_state_mount():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "${MYCELIUM_PUBLISH_ADDRESS:-127.0.0.1}:8000:8000" in compose
    assert "./data:/data" in compose
    assert "MYCELIUM_STATE_DIR: /data" in compose
    assert "COPY scripts/__init__.py scripts/preflight.py" in dockerfile
    assert '"--workers"' not in dockerfile
