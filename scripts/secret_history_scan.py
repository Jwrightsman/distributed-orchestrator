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

Findings come at two severities, because two different things get called
"history". A blob that a branch or a tag reaches travels: it is in every clone,
it is on the remote, and it is the urgent case. So does one sitting in the
index, which is a commit away from the same thing. Everything else is local to
this machine -- the reflog is not pushed, an amended-away commit is not pushed,
and what `git add` leaves behind when the commit never happened is not pushed.
Push and clone do not transfer any of it, and `git gc --prune` deletes it.

Both are reported. Only the first sets a failing exit status, because a scan
that fails over objects a fresh clone does not even have is a scan people stop
running.

Read-only. It runs `git cat-file`, `git rev-list`, `git ls-files` and
`git fsck` against the local object database -- no network, no remote, no
writes.
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
from typing import Iterable, Iterator, Sequence

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

#: What a blob is called when no path could be recovered for it.
UNATTRIBUTED_PATH = "(unreachable blob)"

#: How much of its content an orphaned blob has to share with a file at a known
#: path before that path is adopted for it. Measured against this repository's
#: own fifteen orphans: every one matched its true path at 0.75 or better, and
#: the best *wrong* path any of them reached scored 0.24. The floor sits in
#: that gap.
MIN_PATH_SIMILARITY = 0.5

#: ...and the winning path has to be this far clear of the next one. Ambiguity
#: is resolved by adopting nothing, which leaves the blob reported. Suppressing
#: a real credential because it resembled a test fixture is the expensive
#: mistake here; an unattributed line in the output is the cheap one.
MIN_PATH_MARGIN = 0.2

#: Scores under this can never change that decision -- if the winner clears
#: MIN_PATH_SIMILARITY, anything below 0.1 is already further behind than
#: MIN_PATH_MARGIN -- so they are never stored. That is what keeps the
#: bookkeeping to a few paths per orphan rather than one entry per path in the
#: repository.
_SCORE_FLOOR = 0.1

#: Recovery costs one comparison per orphan per file in history. A repository
#: with more loose ends than this is one nobody has ever run `git gc` on. Scan
#: them, but do not try to name them.
MAX_ATTRIBUTED_BLOBS = 500

#: A signature this small is not evidence of anything: two short files sharing
#: four lines of boilerplate are not the same file.
MIN_SIGNATURE_LINES = 5


@dataclass(frozen=True)
class Finding:
    """One secret-shaped string, located well enough to act on."""

    rule: str
    path: str
    line: int
    blob: str
    note: str
    #: True when a branch or tag reaches the blob, or the index holds it --
    #: the two cases that reach a remote. False when only the reflog, a
    #: dangling commit, or nothing at all reaches it: those never leave this
    #: machine. A different severity, not a lesser one.
    reachable: bool = True


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


def scan_text(
    text: str, path: str, blob: str, reachable: bool = True
) -> Iterator[Finding]:
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
            yield Finding(rule, path, number, blob, note, reachable)
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
                reachable,
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


def _absorb(mapping: dict[str, str], lines: Iterable[str]) -> None:
    """Take `<sha> <path>` lines, keeping the first path seen for each blob."""

    for line in lines:
        sha, _, path = line.partition(" ")
        if path and sha not in mapping:
            mapping[sha] = path


def _dangling_paths(repo: Path) -> list[str]:
    """Paths recorded by commits and trees that no ref reaches any more.

    Where one of these survives it names its blobs authoritatively, which beats
    any guess. It recovers nothing for the ordinary case, though, and that is
    worth being clear about: `git add` writes the blob immediately, so work
    that was staged and then amended away leaves a blob no tree ever named.
    For those there is no git metadata left to consult -- see `_PathRecovery`.

    Best effort. A repository too broken to fsck is still worth scanning.
    """

    try:
        report = _git(repo, "fsck", "--unreachable", "--no-progress")
    except RuntimeError:
        return []
    commits: list[str] = []
    trees: list[str] = []
    for line in report.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] != "unreachable":
            continue
        if fields[1] == "commit":
            commits.append(fields[2])
        elif fields[1] == "tree":
            trees.append(fields[2])
    recovered: list[str] = []
    try:
        if commits:
            # --no-walk: each commit's own tree, not its whole ancestry, which
            # is mostly reachable anyway and so already mapped.
            output = _git(repo, "rev-list", "--objects", "--no-walk", *commits)
            recovered.extend(output.decode("utf-8", "replace").splitlines())
        for tree in trees:
            output = _git(repo, "ls-tree", "-r", tree)
            for line in output.decode("utf-8", "replace").splitlines():
                _, _, remainder = line.partition(" blob ")
                sha, _, path = remainder.partition("\t")
                if path:
                    recovered.append(f"{sha} {path}")
    except RuntimeError:
        return recovered
    return recovered


def _index_paths(repo: Path) -> list[str]:
    """Paths for blobs that are staged and not committed."""

    try:
        output = _git(repo, "ls-files", "--stage")
    except RuntimeError:
        return []
    staged: list[str] = []
    for line in output.decode("utf-8", "replace").splitlines():
        metadata, _, path = line.partition("\t")
        fields = metadata.split()
        if path and len(fields) == 3:
            staged.append(f"{fields[1]} {path}")
    return staged


@dataclass(frozen=True)
class BlobIndex:
    """Where the repository puts each blob."""

    #: Blob to one path git records for it, from any source.
    paths: dict[str, str]
    #: The blobs that leave this machine: reachable from a branch or a tag, or
    #: staged in the index and so one commit from being reachable.
    in_history: frozenset[str]


def blob_index(repo: Path) -> BlobIndex:
    """Locate every blob: the path git records for it, and whether it travels.

    Paths come from four sources, most authoritative first: a commit a ref
    reaches, a commit a reflog entry reaches, a commit or tree nothing reaches
    but that survives intact, and the index. A blob none of them names is an
    orphan, and content matching is the only thing left that can name it.

    Only the first and the last of those sources make a blob *travel*. A reflog
    is local to one clone, and a dangling commit is local by definition, so a
    blob that only they reach has the same standing as an orphan: still on this
    disk, never on the remote.
    """

    def lines(*arguments: str) -> list[str]:
        return _git(repo, *arguments).decode("utf-8", "replace").splitlines()

    referenced = lines("rev-list", "--objects", "--all")
    staged = _index_paths(repo)

    paths: dict[str, str] = {}
    _absorb(paths, referenced)
    _absorb(paths, lines("rev-list", "--objects", "--all", "--reflog"))
    _absorb(paths, _dangling_paths(repo))
    _absorb(paths, staged)

    travels: dict[str, str] = {}
    _absorb(travels, referenced)
    _absorb(travels, staged)
    return BlobIndex(paths, frozenset(travels))


def blob_paths(repo: Path) -> dict[str, str]:
    """Every blob git records a path for, mapped to one of those paths."""

    return blob_index(repo).paths


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


def _signature(text: str) -> frozenset[int]:
    """The distinctive lines of a file, hashed, as a set.

    Short lines go: a bare `)`, a docstring fence, `import os` -- half the
    repository shares those and they say nothing about which file this is.
    Hashes rather than the lines themselves, so that holding a signature costs
    a fixed amount per line instead of the length of the line.
    """

    return frozenset(
        hash(stripped)
        for stripped in (line.strip() for line in text.splitlines())
        if len(stripped) > 8
    )


class _PathRecovery:
    """Works out which file an orphaned blob used to be, from its content.

    An orphan is nearly always an earlier draft of a file that is still in
    history, so it shares most of its lines with some version of that file.
    That is a guess, and it is only ever used to decide whether an ignore rule
    already in force applies to it -- so it is deliberately hard to satisfy,
    and an ambiguous match adopts nothing at all.

    The comparison runs the cheap way round. Signatures are built for the
    orphans, which are few, and every file in history is scored against them as
    it streams past, so nothing but the orphans is ever held in memory.
    """

    def __init__(self, signatures: dict[str, frozenset[int]]):
        self._signatures = {
            sha: signature
            for sha, signature in signatures.items()
            if len(signature) >= MIN_SIGNATURE_LINES
        }
        self._scores: dict[str, dict[str, float]] = {
            sha: {} for sha in self._signatures
        }

    def observe(self, path: str, text: str) -> None:
        """Score one file in history against every orphan."""

        if not self._signatures:
            return
        other = _signature(text)
        if len(other) < MIN_SIGNATURE_LINES:
            return
        for sha, signature in self._signatures.items():
            shared = len(signature & other)
            if not shared:
                continue
            score = shared / len(signature | other)
            if score < _SCORE_FLOOR:
                continue
            best = self._scores[sha]
            if score > best.get(path, 0.0):
                best[path] = score

    def resolve(self) -> dict[str, str]:
        """For each orphan one file matched unambiguously, that file's path."""

        recovered: dict[str, str] = {}
        for sha, scores in self._scores.items():
            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
            if not ranked:
                continue
            path, best = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
            if best >= MIN_PATH_SIMILARITY and best - runner_up >= MIN_PATH_MARGIN:
                recovered[sha] = path
        return recovered


def _decoded(content: bytes) -> str | None:
    """The blob as text, or None if it is binary or not UTF-8."""

    if b"\x00" in content[:8192]:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_repository(repo: Path | None = None) -> list[Finding]:
    """Every secret-shaped string in every blob this repository holds."""

    root = repo or REPO_ROOT
    located = blob_index(root)
    paths = located.paths
    blobs = candidate_blobs(root)
    orphans = [sha for sha in blobs if sha not in paths]
    named = [sha for sha in blobs if sha in paths]

    signatures: dict[str, frozenset[int]] = {}
    if len(orphans) <= MAX_ATTRIBUTED_BLOBS:
        for sha, content in _batch_contents(root, orphans):
            text = _decoded(content)
            if text is not None:
                signatures[sha] = _signature(text)
    recovery = _PathRecovery(signatures)

    findings: list[Finding] = []
    for sha, content in _batch_contents(root, named):
        text = _decoded(content)
        if text is None:
            continue
        path = paths[sha]
        # Scored before the ignore check, not after: an orphaned draft of an
        # ignored file can only inherit that path if the file was measured too.
        recovery.observe(path, text)
        if _IGNORED_PATH.search(path):
            continue
        findings.extend(
            scan_text(text, path, sha[:12], reachable=sha in located.in_history)
        )

    recovered = recovery.resolve()
    for sha, content in _batch_contents(root, orphans):
        text = _decoded(content)
        if text is None:
            continue
        path = recovered.get(sha, UNATTRIBUTED_PATH)
        if _IGNORED_PATH.search(path):
            continue
        findings.extend(scan_text(text, path, sha[:12], reachable=False))
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


_LOCAL_ONLY_ADVICE = """\
These live on this machine only. Nothing reaches them, so `git push` and
`git clone` do not carry them and the remote has never had them. They are
still readable by anything that can read this directory.

  1. If the value was ever pushed on a branch, or shown in a screen share,
     rotate it as above. That an object is unreachable *now* says nothing
     about where it has already been.
  2. Otherwise this is disk hygiene, not an incident. `git gc --prune=now`
     drops every object no ref and no reflog entry reaches, these included.\
"""


def _locations(findings: Sequence[Finding]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        lines.append(f"  {finding.path}:{finding.line}  [{finding.rule}]")
        lines.append(f"    {finding.note} (blob {finding.blob})")
    return lines


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
    in_history = [finding for finding in findings if finding.reachable]
    local_only = [finding for finding in findings if not finding.reachable]
    lines: list[str] = []
    if in_history:
        lines += [
            f"{len(in_history)} secret-shaped value(s) found in this "
            "repository's history.",
            "",
            "No value is printed below. Locations only.",
            "",
        ]
        lines += _locations(in_history)
        lines += ["", _ADVICE]
    if local_only:
        if in_history:
            lines += ["", "-" * 70, ""]
        lines += [
            f"{len(local_only)} secret-shaped value(s) found in unreachable "
            "objects.",
            "",
            "No value is printed below. Locations only. Where a path is shown "
            "it was",
            "recovered by matching content, and names the file the blob was a "
            "draft of.",
            "",
        ]
        lines += _locations(local_only)
        lines += ["", _LOCAL_ONLY_ADVICE]
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
    in_history = [finding for finding in findings if finding.reachable]
    if args.json_output:
        print(
            json.dumps(
                {
                    # Tracks the exit status: unreachable objects are reported
                    # in `findings` but do not make the scan fail.
                    "ok": not in_history,
                    "findings": [asdict(finding) for finding in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render(findings))
    return 1 if in_history else 0


if __name__ == "__main__":
    raise SystemExit(main())
