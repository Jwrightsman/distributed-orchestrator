"""The CLI must express the canonical remote-dispatch consent contract."""

import pytest

import cli


def _request(argv: list[str], *, task: str = "build it"):
    options, remaining = cli.parse_execution_args(argv)
    assert remaining == [task]
    return cli.build_execution_request(
        task,
        strategy=options.strategy,
        candidates=options.candidates,
        placement=options.placement,
        allow_remote=options.allow_remote,
        confidentiality=options.confidentiality,
        approved_node_ids=options.approved_node_ids,
    )


def test_cli_defaults_to_local_only_without_remote_consent():
    request = _request(["build it"])
    assert request.placement == "local"
    assert request.confidentiality == "local_only"
    assert request.remote_dispatch_consent is False


def test_distributed_cli_requires_explicit_consent(capsys):
    with pytest.raises(SystemExit, match="2"):
        cli.parse_execution_args(["--placement", "distributed", "build it"])
    assert "requires --allow-remote" in capsys.readouterr().err


def test_distributed_consent_defaults_to_trusted_guild():
    request = _request(["--placement", "distributed", "--allow-remote", "build it"])
    assert request.placement == "distributed"
    assert request.confidentiality == "trusted_guild"
    assert request.remote_dispatch_consent is True


def test_auto_becomes_remote_capable_only_with_explicit_consent():
    local_auto = _request(["--placement", "auto", "build it"])
    assert local_auto.confidentiality == "local_only"
    assert local_auto.remote_dispatch_consent is False

    remote_auto = _request(["--placement", "auto", "--allow-remote", "build it"])
    assert remote_auto.confidentiality == "trusted_guild"
    assert remote_auto.remote_dispatch_consent is True


def test_approved_nodes_are_normalized_into_execution_requirements():
    request = _request(
        [
            "--placement",
            "distributed",
            "--allow-remote",
            "--confidentiality",
            "approved_nodes",
            "--approved-node",
            "worker-a",
            "--approved-node",
            "worker-b",
            "build it",
        ]
    )
    assert request.requirements.approved_node_ids == ["worker-a", "worker-b"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["--placement", "auto", "--confidentiality", "trusted_guild", "build it"],
            "requires --allow-remote",
        ),
        (
            ["--placement", "local", "--allow-remote", "build it"],
            "requires --placement auto|distributed",
        ),
        (
            [
                "--placement",
                "distributed",
                "--allow-remote",
                "--confidentiality",
                "local_only",
                "build it",
            ],
            "non-local confidentiality",
        ),
        (
            [
                "--placement",
                "distributed",
                "--allow-remote",
                "--confidentiality",
                "approved_nodes",
                "build it",
            ],
            "requires at least one --approved-node",
        ),
        (
            ["--approved-node", "worker-a", "build it"],
            "requires --confidentiality approved_nodes",
        ),
    ],
)
def test_invalid_remote_cli_combinations_fail_before_inference(argv, message, capsys):
    with pytest.raises(SystemExit, match="2"):
        cli.parse_execution_args(argv)
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("strategy", "extra"),
    [
        ("direct", []),
        ("ensemble", ["--candidates", "3"]),
        ("dag", []),
    ],
)
def test_every_existing_strategy_can_be_distributed_with_consent(strategy, extra):
    request = _request(
        [
            "--strategy",
            strategy,
            "--placement",
            "distributed",
            "--allow-remote",
            *extra,
            "build it",
        ]
    )
    assert request.strategy == strategy
    assert request.placement == "distributed"
    assert request.remote_dispatch_consent is True
    assert request.confidentiality == "trusted_guild"
