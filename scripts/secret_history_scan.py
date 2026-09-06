#!/usr/bin/env python3
"""Look for anything secret-shaped that was ever committed to this repository.

Deleting a credential in a later commit changes nothing: the old blob is still
in the repository, still in every clone, and still on GitHub. So this scans
**history**, not the working tree -- every blob the object database holds,
including ones no branch points at any more.

    python3 scripts/secret_history_scan.py
    python3 scripts/secret_history_scan.py --json

If it finds something, the order of operations is not negotiable:

  1. **Rotate the credential.** Right now, before anything else. A value that
     was ever pushed is a value strangers have had the opportunity to read,
     and rewriting history does not un-read it.
  2. Re-issue what depends on it: a new invitation code goes to your workers,
     a new viewer key logs everyone's browser out, a new pitch key goes to
     whoever pitches.
  3. Only then decide whether to rewrite history. It is disruptive, it breaks
     every existing clone, and it is the *least* urgent step. If the
     repository is public, treat step 3 as cosmetic.

It prints no secret value: a finding names the file, the line number, and the
rule that fired, and nothing else.

Read-only. It runs `git cat-file` against the local object database and
nothing else -- no network, no remote, no writes.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Blobs larger than this are model output, fixtures, or binaries. A credential
#: is short, and scanning a 40 MB blob line by line is not worth the minute.
MAX_BLOB_BYTES = 2 * 1024 * 1024

#: Entropy floor for the generic rule, in bits per character. English prose
#: sits near 3, lowercase hex near 4, base64 of random bytes near 6. Measured
#: over a short string this under-reports, so the floor is deliberately below
#: hex: the assignment shape below is what makes the rule precise, and this
#: only has to exclude ordinary words.
MIN_ENTROPY_PER_CHARACTER = 3.6


@dataclass(frozen=True)
class Finding:
    """One secret-shaped string, located well enough to act on."""

    rule: str
    path: str
    line: int
    blob: str
    note: str


# -- Rules -------------------------------------------------------------------
#
# Two kinds. Named rules look for this project's own authorities and for
# credential formats that announce themselves. The generic rule looks for a
# long high-entropy token next to a word that suggests it is a secret, which is
# what catches the case nobody anticipated.

_NAMED_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "mycelium_authority",
        re.compile(
            r'"(node_secret|pitch_key|viewer_key)"\s*:\s*"([^"]{16,})"',
        ),
        "one of this deployment's three authorities, with a value",
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "a private key",
    ),
    (
        "provider_api_key",
        re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})\b"),
        "an API key in a format its issuer publishes",
    ),
    (
        "enrollment_credential",
        re.compile(r'"enrollment_credential"\s*:\s*"([^"]{16,})"'),
        "a worker's durable enrollment credential",
    ),
)

#: A field whose *name* says it holds a secret, assigned a long random-looking
#: value. Requiring the name and the value to be adjacent is what makes this
#: rule usable: an earlier version looked for a high-entropy token anywhere on
#: a line that merely mentioned "key", and it reported ten hits on a file of
#: benchmark results where `keyHandlers` supplied the word and a git worktree
#: name supplied the entropy. A scanner that cries wolf is one people stop
#: running, which is worse than not having it.
_GENERIC_ASSIGNMENT = re.compile(
    r"""
    [A-Za-z0-9_.\[\]-]*                        # an optional prefix, so
    (?:secret|token|key|password|passwd|credential|auth)  # ...api_key, NODE_SECRET
    [A-Za-z0-9_.\[\]-]*
    ["']?                                      # closing quote of a JSON name
    \s*[:=]\s*                                 # : in JSON, = in shell or Python
    ["']?                                      # opening quote of the value
    (?P<value>[A-Za-z0-9+/_-]{24,}={0,2})
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Values that are meant to be in the repository. A placeholder in the
#: documentation is documentation, and a test fixture's fake credential is a
#: test fixture; reporting them trains the reader to ignore this tool.
_PLACEHOLDER = re.compile(
    r"(changeme|change-me|your-|example|placeholder|replace|<[^>]+>|"
    r"independent-random|xxxx|0{16,}|a{16,}|test[-_]?secret|fake|dummy|sample)",
    re.IGNORECASE,
)

#: Paths whose whole job is to describe or test credential handling.
_IGNORED_PATH = re.compile(
    r"(^tests/|^evals/|^docs/audits/|^scripts/secret_history_scan\.py$|"
    r"(^|/)(SPRINT_|HANDOFF|ROADMAP|README|CHANGELOG)|\.lock$|"
    r"(^|/)(certifi|cacert)\.pem$)"
)


_HEX_TOKEN = re.compile(r"[0-9a-f]{32,}")
_SEGMENT = re.compile(r"[-_]")

#: One word a person could have typed: a word, a capitalised word, or an
#: acronym. `version`, `Extraction`, `SSL`.
_WORD_PIECE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+")

#: Where a name changes word without a separator to announce it. The first
#: branch is the ordinary camelCase hump (`keyHandlers`); the second is the end
#: of an acronym that runs into the next word (`SSLCert` -> `SSL` + `Cert`),
#: without which `SSLCertVerificationError` does not decompose and gets
#: reported as a credential.
_HUMP = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: Mean length of a word piece, at or above which a value reads as prose.
#: Measured, not guessed. Over 300,000 digit-free tokens the mean piece length
#: is 2.47 at the median and 4.20 at the 99.9th percentile. Of the 73 names in
#: this repository that this floor actually decides -- the ones that read as
#: words and mix case, so nothing else would reject them -- the lowest is 4.14
#: (`TestEmptyExtractionIsNotAPass`). 4.0 is the highest floor that still
#: rejects all 73, and it is what puts the miss rate on a generated credential
#: near one in a million. The margin above it is thin by design: a false
#: positive costs one triage, a false negative is a leaked credential this
#: tool said nothing about.
MIN_MEAN_WORD_LENGTH = 4.0


def _word_pieces(value: str) -> list[str]:
    """Split where someone writing a name would have put a word boundary."""

    pieces: list[str] = []
    for segment in _SEGMENT.split(value):
        if segment:
            pieces.extend(piece for piece in _HUMP.split(segment) if piece)
    return pieces


def reads_as_words(value: str) -> bool:
    """Does this decompose into words a person would have typed?

    Both halves are needed. `normalize_credential_version` decomposes into
    three words, and so, piece by piece, does a random string -- the
    difference is that the random one's pieces are two characters long.
    """

    pieces = _word_pieces(value)
    if not pieces or not all(_WORD_PIECE.fullmatch(piece) for piece in pieces):
        return False
    total = sum(len(piece) for piece in pieces)
    return total / len(pieces) >= MIN_MEAN_WORD_LENGTH


def looks_random(value: str) -> bool:
    """Does this look generated rather than typed?

    Entropy alone does not separate the two at these lengths. The false
    positives that forced this function into existence were all *words*:
    `normalize_credential_version` on the right of an `=`, the constant
    `DEADLINE_COMPLETION_SUBJECT`, and the ADR's illustrative
    `"plaintext-returned-only-to-the-worker"`. Each scored above any entropy
    floor low enough to still catch a hex token.

    What separates them is shape: a generated credential does not decompose
    into dictionary words at its separators and humps.

    This used to also require a digit, which was a hole rather than a rule.
    `secrets.token_urlsafe(32)` -- the generator this project issues its own
    authorities with -- draws 43 characters from a 64-symbol alphabet, so
    about one in 1,600 of them contains no digit anywhere. Every one of those
    was a real credential this scanner called ordinary and walked past. The
    word test below replaces the digit test and costs no precision: across
    the 2,576 names and captured values in this repository it reports nothing
    the digit rule was not already reporting, and it misses roughly one
    generated credential in a million rather than one in 1,600.
    """

    if not any(character.isalpha() for character in value):
        return False
    if reads_as_words(value):
        return False  # words joined by -, _, or a capital letter
    if _HEX_TOKEN.fullmatch(value):
        return True
    has_upper = any(character.isupper() for character in value)
    has_lower = any(character.islower() for character in value)
    return (has_upper and has_lower) or any(
        character in value for character in "+/="
    )


def entropy_per_character(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def scan_text(text: str, path: str, blob: str) -> Iterator[Finding]:
    """Every secret-shaped string in one blob."""

    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4096:
            continue
        matched_named = False
        for rule, pattern, note in _NAMED_RULES:
            match = pattern.search(line)
            if not match:
                continue
            captured = match.groups()[-1] if match.groups() else ""
            if captured and _PLACEHOLDER.search(captured):
                continue
            matched_named = True
            yield Finding(rule, path, number, blob, note)
        if matched_named:
            continue
        for match in _GENERIC_ASSIGNMENT.finditer(line):
            candidate = match.group("value")
            if _PLACEHOLDER.search(candidate) or not looks_random(candidate):
                continue
            if entropy_per_character(candidate) < MIN_ENTROPY_PER_CHARACTER:
                continue
            yield Finding(
                "secret_named_field_with_a_value",
                path,
                number,
                blob,
                "a field whose name says it holds a secret, with a "
                "random-looking value assigned to it",
            )
            break


# -- The object database -----------------------------------------------------


def _git(repo: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def blob_paths(repo: Path) -> dict[str, str]:
    """Map every blob that a reachable commit named to one of its paths."""

    mapping: dict[str, str] = {}
    output = _git(repo, "rev-list", "--objects", "--all")
    for line in output.decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in mapping:
            mapping[sha] = path
    return mapping


def candidate_blobs(repo: Path) -> list[str]:
    """Every blob in the object database, including unreachable ones."""

    output = _git(
        repo,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
    )
    blobs: list[str] = []
    for line in output.decode("ascii", "replace").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] != "blob":
            continue
        try:
            size = int(fields[2])
        except ValueError:
            continue
        if 0 < size <= MAX_BLOB_BYTES:
            blobs.append(fields[0])
    return blobs


def _batch_contents(repo: Path, blobs: Sequence[str]) -> Iterator[tuple[str, bytes]]:
    """Stream blob contents out of one `git cat-file --batch` invocation."""

    if not blobs:
        return
    payload = ("\n".join(blobs) + "\n").encode("ascii")
    stream = _git(repo, "cat-file", "--batch", stdin=payload)
    offset = 0
    total = len(stream)
    while offset < total:
        newline = stream.find(b"\n", offset)
        if newline == -1:
            return
        header = stream[offset:newline].decode("ascii", "replace").split()
        offset = newline + 1
        if len(header) != 3:
            return
        sha, size = header[0], int(header[2])
        yield sha, stream[offset : offset + size]
        offset += size + 1  # the trailing newline git adds after each object


def scan_repository(repo: Path | None = None) -> list[Finding]:
    """Every secret-shaped string in every blob this repository holds."""

    root = repo or REPO_ROOT
    paths = blob_paths(root)
    findings: list[Finding] = []
    for sha, content in _batch_contents(root, candidate_blobs(root)):
        if b"\x00" in content[:8192]:
            continue  # binary
        path = paths.get(sha, "(unreachable blob)")
        if _IGNORED_PATH.search(path):
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, path, sha[:12]))
    return findings


_ADVICE = """\
Rotate first. Everything else is secondary.

  1. Generate replacements without printing them:
       python3 -c "from config import ensure_trusted_alpha_config as e; \\
                   e('data/config.json')"
  2. Send the new invitation code to your workers. The old one stops working
     for a new bootstrap the moment you restart the coordinator.
  3. Revoke any enrollment you are unsure about, one at a time:
       python3 scripts/node_enrollment_admin.py list
       python3 scripts/node_enrollment_admin.py revoke ENROLLMENT-ID \\
           --reason "credential found in git history"
  4. Only now consider rewriting history. It breaks every existing clone and
     does not un-publish anything already fetched. If the repository is
     public, this step is cosmetic.

See docs/SECRET_ROTATION.md for what breaks for already-enrolled workers.\
"""


def render(findings: Sequence[Finding]) -> str:
    if not findings:
        return (
            "No secret-shaped value found in any blob in this repository's "
            "history.\n\n"
            "This is evidence, not a guarantee. The scan knows this project's "
            "own authority names, a handful of key formats their issuers "
            "publish, and fields whose name says 'secret' assigned a value "
            "that looks generated. A credential chosen to look like an "
            "ordinary word would slip past it -- as it would past anyone "
            "reading the diff, which is the other reason to let the generator "
            "pick them."
        )
    lines = [
        f"{len(findings)} secret-shaped value(s) found in this repository's "
        "history.",
        "",
        "No value is printed below. Locations only.",
        "",
    ]
    for finding in findings:
        lines.append(f"  {finding.path}:{finding.line}  [{finding.rule}]")
        lines.append(f"    {finding.note} (blob {finding.blob})")
    lines.append("")
    lines.append(_ADVICE)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan git history for anything secret-shaped."
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    try:
        findings = scan_repository(args.repo)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json_output:
        print(
            json.dumps(
                {
                    "ok": not findings,
                    "findings": [asdict(finding) for finding in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
