#!/usr/bin/env python3
"""List, revoke, or rotate durable Mycelium node enrollments locally.

This command operates on the coordinator state directory.  It never prints or
lists enrollment credentials or their digests.  Rotation writes the replacement
worker identity to an owner-protected file instead of returning the credential
through stdout.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from coordinator_lock import default_state_dir  # noqa: E402
import node_enrollments  # noqa: E402
from worker_identity import (  # noqa: E402
    IDENTITY_VERSION,
    WorkerIdentity,
    WorkerIdentityError,
    load_worker_identity,
    normalize_coordinator,
    normalize_enrollment_id,
    write_new_worker_identity,
)


class EnrollmentAdminError(RuntimeError):
    """An enrollment administration request is unsafe or incomplete."""


def _database_path(state_dir: Path | str | None) -> Path:
    root = Path(state_dir) if state_dir is not None else default_state_dir()
    root = root.expanduser()
    if not root.exists() or not root.is_dir():
        raise EnrollmentAdminError(f"state directory does not exist: {root}")
    return root / "events.db"


def _store(state_dir: Path | str | None):
    store = node_enrollments.NodeEnrollmentStore(_database_path(state_dir))
    store.migrate()
    return store


def _record_metadata(record) -> dict[str, object]:
    # Explicit allowlist: a future store field can never accidentally make a
    # credential digest part of this command's output.
    public = record.public_metadata()
    return {
        name: public.get(name)
        for name in (
            "enrollment_id",
            "node_id",
            "status",
            "credential_version",
            "created_at",
            "rotated_at",
            "revoked_at",
            "revocation_reason",
            "last_registered_at",
        )
    }


def list_enrollments(store, *, json_output: bool = False) -> None:
    records = list(store.list())
    if json_output:
        print(json.dumps([_record_metadata(record) for record in records], indent=2))
        return
    if not records:
        print("No node enrollments.")
        return
    print(
        "ENROLLMENT_ID                         STATUS   NODE_ID  "
        "LAST_REGISTERED_AT"
    )
    for record in records:
        last_registered = record.public_metadata().get("last_registered_at") or "-"
        print(
            f"{record.enrollment_id}  {record.status:<8} "
            f"{record.node_id}  {last_registered}"
        )


def _bounded_reason(value: str) -> str:
    reason = value.strip()
    maximum = int(getattr(node_enrollments, "REVOCATION_REASON_MAX_LENGTH", 500))
    if len(reason) > maximum:
        raise EnrollmentAdminError(
            f"revocation reason must be {maximum} characters or fewer"
        )
    if any(character in reason for character in ("\x00", "\r", "\n")):
        raise EnrollmentAdminError("revocation reason must be one line without NUL bytes")
    return reason or "operator revocation"


def revoke_enrollment(store, enrollment_id: str, *, reason: str) -> None:
    normalized = normalize_enrollment_id(enrollment_id)
    record = store.revoke(normalized, _bounded_reason(reason))
    print(f"Enrollment revoked: {record.enrollment_id} ({record.node_id})")


def _new_credential() -> str:
    generator = getattr(node_enrollments, "new_enrollment_credential", None)
    if generator is None:
        generator = getattr(node_enrollments, "generate_enrollment_credential", None)
    if generator is not None:
        return str(generator())
    # Defensive fallback for a mixed-version checkout. The store still applies
    # its own strict validator before changing durable authentication state.
    return secrets.token_urlsafe(32)


def _rotation_identity(
    output: Path,
    *,
    coordinator: str,
    record,
    resume_existing: bool,
) -> tuple[WorkerIdentity, bool]:
    if output.exists() or output.is_symlink():
        if not resume_existing:
            raise EnrollmentAdminError(
                "identity output already exists; choose an unused path, or use "
                "--resume-existing only to resolve a previously prepared rotation"
            )
        identity = load_worker_identity(
            output,
            coordinator=coordinator,
            node_id=record.node_id,
        )
        if identity.enrollment_id != record.enrollment_id:
            raise EnrollmentAdminError(
                "existing identity output belongs to a different enrollment"
            )
        if identity.credential_version is None:
            raise EnrollmentAdminError(
                "existing identity output has no prepared credential version"
            )
        if identity.credential_version < record.credential_version:
            raise EnrollmentAdminError(
                "existing identity output is stale and cannot be rotated backward"
            )
        if identity.credential_version > record.credential_version + 1:
            raise EnrollmentAdminError(
                "existing identity output targets an invalid future credential version"
            )
        # Reusing an already-written candidate makes a retry converge if the
        # prior SQLite commit outcome was ambiguous.
        return identity, False
    identity = WorkerIdentity(
        version=IDENTITY_VERSION,
        coordinator=normalize_coordinator(coordinator),
        node_id=record.node_id,
        enrollment_id=record.enrollment_id,
        credential_version=record.credential_version + 1,
        enrollment_credential=_new_credential(),
    )
    try:
        write_new_worker_identity(output, identity)
    except WorkerIdentityError as exc:
        raise EnrollmentAdminError(
            "identity output was created concurrently; inspect it and use "
            "--resume-existing only if it is the intended prepared rotation"
        ) from exc
    return identity, True


def rotate_enrollment(
    store,
    enrollment_id: str,
    *,
    coordinator: str,
    identity_output: Path,
    resume_existing: bool = False,
) -> None:
    normalized = normalize_enrollment_id(enrollment_id)
    record = store.get(normalized)
    if record is None:
        raise EnrollmentAdminError("node enrollment was not found")
    if record.status != "active":
        raise EnrollmentAdminError("revoked node enrollment cannot be rotated")
    output = identity_output.expanduser()
    identity, created = _rotation_identity(
        output,
        coordinator=coordinator,
        record=record,
        resume_existing=resume_existing,
    )
    try:
        result = store.rotate(
            normalized,
            identity.enrollment_credential,
            expected_credential_version=identity.credential_version - 1,
        )
    except Exception:
        if created:
            print(
                "Rotation was not confirmed. Keep the private identity output and "
                "rerun the same command with --resume-existing to resolve an "
                "ambiguous commit.",
                file=sys.stderr,
            )
        raise
    if result.record.credential_version != identity.credential_version:
        raise EnrollmentAdminError(
            "replacement identity version does not match the durable rotation"
        )
    status = "rotated" if result.rotated else "already rotated"
    print(
        f"Enrollment {status}: {result.record.enrollment_id} "
        f"({result.record.node_id})"
    )
    print(f"Replacement identity written privately to: {output.resolve()}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Mycelium coordinator state directory (default: MYCELIUM_STATE_DIR or .)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list non-secret enrollment metadata")
    listing.add_argument("--json", action="store_true", dest="json_output")

    revoke = commands.add_parser("revoke", help="durably revoke one enrollment")
    revoke.add_argument("enrollment_id")
    revoke.add_argument("--reason", default="operator revocation")

    rotate = commands.add_parser("rotate", help="rotate one enrollment credential")
    rotate.add_argument("enrollment_id")
    rotate.add_argument("--coordinator", required=True)
    rotate.add_argument("--identity-output", required=True, type=Path)
    rotate.add_argument(
        "--resume-existing",
        action="store_true",
        help="reuse a private output prepared by an earlier ambiguous rotation attempt",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = _store(args.state_dir)
        if args.command == "list":
            list_enrollments(store, json_output=args.json_output)
        elif args.command == "revoke":
            revoke_enrollment(store, args.enrollment_id, reason=args.reason)
        else:
            rotate_enrollment(
                store,
                args.enrollment_id,
                coordinator=args.coordinator,
                identity_output=args.identity_output,
                resume_existing=args.resume_existing,
            )
    except (
        EnrollmentAdminError,
        WorkerIdentityError,
        node_enrollments.NodeEnrollmentError,
        OSError,
    ) as exc:
        print(f"Node enrollment administration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
