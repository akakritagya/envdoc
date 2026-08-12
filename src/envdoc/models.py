"""The data model every other module speaks in.

The shape that matters here is the two-level split between an occurrence and a
variable. A Finding is one reference at one location; a Variable is one name
with everything known about it, everywhere it appears. Aggregating the first
into the second is where severity gets decided, and it is the reason this tool
can report things a per-line scanner structurally cannot -- two files
disagreeing about a default, for instance, is invisible until you have looked
at both.

Three sources feed in, and the audit is the three-way intersection of them:

    code         something reads the variable
    example      .env.example documents it
    deployment   a manifest actually sets it

Two-way tools compare the first two. The third is the one that catches a
variable the code requires, the example documents, and the compose file never
sets -- which works locally and dies in the container.

Determinism is a property of this module, not just of the renderers. Paths are
PurePosixPath relative to the scan root so output cannot leak a machine's
identity or a filesystem's separator, and every collection that reaches
rendering is an ordered tuple rather than a set.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class SourceKind(StrEnum):
    """Which of the three axes an occurrence sits on."""

    CODE = "code"
    EXAMPLE = "example"
    DEPLOYMENT = "deployment"


class Provider(StrEnum):
    """The specific parser that produced an occurrence.

    Finer-grained than SourceKind on purpose: when a finding looks wrong, the
    first question is which parser produced it, and "code" does not answer that.
    """

    PYTHON_AST = "python_ast"
    PYTHON_SETTINGS = "python_settings"
    TS_JS = "ts_js"
    GO = "go"
    RUST = "rust"
    DOTENV_EXAMPLE = "dotenv_example"
    DOCKER_COMPOSE = "docker_compose"
    DOCKERFILE = "dockerfile"
    GITHUB_ACTIONS = "github_actions"
    FLY_TOML = "fly_toml"
    K8S_MANIFEST = "k8s_manifest"


class Confidence(StrEnum):
    """How much the name itself can be trusted.

    Only two members. A reference whose name cannot be resolved statically is
    not a low-confidence Finding -- it is a DynamicRef, which has no name at
    all and therefore never becomes a Variable. Encoding that as a third
    confidence level would invite code to read `.name` on something that has
    none.
    """

    EXACT = "exact"
    HEURISTIC = "heuristic"


class Status(StrEnum):
    """The verdict for one variable, from the three-way audit in the README."""

    OK = "ok"
    UNDOCUMENTED = "undocumented"
    STALE = "stale"
    UNSET_IN_DEPLOYMENT = "unset_in_deployment"
    ORPHAN_DEPLOYMENT = "orphan_deployment"


class FailOn(StrEnum):
    """Which statuses make `envdoc check` exit non-zero.

    A threshold names a *set* of statuses rather than a point on a severity
    scale, because the statuses are not totally ordered by how much anyone
    cares about them -- an orphaned deployment key and a stale example entry
    are both mild, and neither is milder than the other in a way worth
    encoding.
    """

    UNDOCUMENTED = "undocumented"
    UNSET = "unset"
    STALE = "stale"
    ANY = "any"


# Cumulative on purpose: each threshold gates everything the looser ones gate.
# UNSET_IN_DEPLOYMENT is special-cased in has_drift -- see the comment there.
_GATING_STATUSES: dict[FailOn, frozenset[Status]] = {
    FailOn.UNDOCUMENTED: frozenset({Status.UNDOCUMENTED}),
    FailOn.UNSET: frozenset({Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT}),
    FailOn.STALE: frozenset({Status.UNDOCUMENTED, Status.UNSET_IN_DEPLOYMENT, Status.STALE}),
    FailOn.ANY: frozenset(
        {
            Status.UNDOCUMENTED,
            Status.UNSET_IN_DEPLOYMENT,
            Status.STALE,
            Status.ORPHAN_DEPLOYMENT,
        }
    ),
}

# has_drift indexes this dict directly, so a FailOn left out would raise
# KeyError from inside `check` rather than failing at import time, where it is
# actually cheap to notice.
assert set(_GATING_STATUSES) == set(FailOn), "every FailOn threshold must have a gating set"
# ANY is the only threshold required to cover every status: it is the "fail on
# anything" case, and any status left out of it would be a status that no
# threshold can ever gate on.
assert _GATING_STATUSES[FailOn.ANY] == set(Status) - {Status.OK}, (
    "FailOn.ANY must gate on every status except OK"
)


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One location where a variable was mentioned, on any of the three axes.

    `required` and `default` describe a *call site* and are meaningful only
    when `source` is CODE. A line in .env.example is not "required", and a
    compose `environment:` key has no fallback to record. Non-code occurrences
    carry `required=False, default=None`, and aggregation reads both fields
    only from code occurrences -- see aggregate.py.

    Deliberately not `order=True`. Ordering a dataclass compares every field in
    turn, so two occurrences agreeing on file, line and column would fall
    through to comparing `default`, and `None < "8000"` raises TypeError.
    Sorting is done with an explicit key instead.
    """

    file: PurePosixPath
    line: int
    column: int
    source: SourceKind
    provider: Provider
    required: bool
    default: str | None


def sort_key(occurrence: Occurrence) -> tuple[str, int, int]:
    """The one true ordering for occurrences: where they are, nothing else.

    Exposed rather than inlined so every call site sorts identically. Byte-
    identical output depends on it.
    """
    return (str(occurrence.file), occurrence.line, occurrence.column)


@dataclass(frozen=True, slots=True)
class Finding:
    """One statically-resolved reference at one location."""

    name: str
    occurrence: Occurrence
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class DynamicRef:
    """A reference whose name cannot be resolved, e.g. `os.getenv(key_var)`.

    Has no name, so it never becomes a Variable and never counts toward drift
    by default. It is reported rather than guessed at: a tool that invents a
    name for this is a tool people stop trusting the moment they notice.
    """

    occurrence: Occurrence
    expression: str


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """What one parser made of one file.

    Every extractor returns this shape, whichever of the three axes it reads,
    so discovery can concatenate results from a Python file, an .env.example
    and a compose file without caring which produced what.

    Carrying `warnings` rather than printing them is what keeps the parsers
    below the CLI layer: an unparseable file is worth telling the user about,
    but only the CLI knows whether --quiet is in force.
    """

    findings: tuple[Finding, ...]
    dynamic: tuple[DynamicRef, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Variable:
    """One environment variable, aggregated across every occurrence everywhere.

    `status` is the headline verdict and `statuses` the full set, because one
    variable can carry more than one problem at once -- undocumented *and*
    unset in deployment is a common pairing, and collapsing it to whichever
    happens to be worse would hide half the fix.
    """

    name: str
    required: bool
    confidence: Confidence
    status: Status
    statuses: frozenset[Status]
    defaults: tuple[str, ...]
    occurrences: tuple[Occurrence, ...]
    documented_in_example: bool
    deployment_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Report:
    """Everything one scan produced. The only object crossing into rendering."""

    root: PurePosixPath
    variables: tuple[Variable, ...]
    dynamic: tuple[DynamicRef, ...]
    warnings: tuple[str, ...]
    files_scanned: int
    deployment_files_found: tuple[str, ...]

    def by_status(self, status: Status) -> tuple[Variable, ...]:
        """Every variable carrying `status`, including as a secondary status."""
        return tuple(var for var in self.variables if status in var.statuses)

    def has_drift(self, threshold: FailOn) -> bool:
        """Whether anything at or above `threshold` was found.

        The single thing `envdoc check` keys off. UNSET_IN_DEPLOYMENT gates
        only when the variable is required: a variable with a fallback that no
        manifest sets degrades to its default, which is worth printing but not
        worth breaking a build over. One with no fallback takes the process
        down on boot.
        """
        gating = _GATING_STATUSES[threshold]
        for variable in self.variables:
            for status in variable.statuses:
                if status not in gating:
                    continue
                if status is Status.UNSET_IN_DEPLOYMENT and not variable.required:
                    continue
                return True
        return False
