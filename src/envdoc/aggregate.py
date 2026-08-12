"""Collapse per-occurrence Findings into per-name Variables.

This is the step that makes envdoc more than a line scanner. Everything it
produces is a statement about a *name* rather than about a location, and some
of those statements cannot be made from any single location:

    - PORT is required, because one of its four call sites has no fallback,
      even though the other three do
    - PORT has conflicting defaults, because src/api.py says 8000 and
      src/worker.py says 3000

The second one is a real bug -- two halves of the same system disagreeing about
what happens when the variable is unset -- and it is invisible to a tool that
reports each call site independently.

Severity always resolves to the maximum, never the average. One code path that
crashes without the variable is enough to call it required; the fact that three
other paths cope is not mitigation.

What this module deliberately does *not* decide is Status. That needs the
three-way set algebra in audit.py, which knows about the example file and the
deployment manifests. Variables come out of here with placeholder status and an
empty status set, and audit.py fills them in.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from envdoc.models import (
    Confidence,
    Finding,
    Occurrence,
    SourceKind,
    Status,
    Variable,
    sort_key,
)


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """Variables plus anything noticed while building them."""

    variables: tuple[Variable, ...]
    warnings: tuple[str, ...]


def _best_confidence(findings: Iterable[Finding]) -> Confidence:
    """EXACT beats HEURISTIC.

    If any parser resolved the name exactly, the name is exact -- a second,
    weaker sighting of the same name does not make it less certain.
    """
    return (
        Confidence.EXACT
        if any(f.confidence is Confidence.EXACT for f in findings)
        else Confidence.HEURISTIC
    )


def cite_defaults(occurrences: Iterable[Occurrence]) -> str:
    """The first occurrence of each distinct default among `occurrences`, cited.

    Empty when fewer than two distinct defaults appear -- there is nothing to
    cite when call sites agree, or when at most one of them set a default at
    all. Shared between the conflict warning below and `sync.py`'s comment for
    the same disagreement, so the file and the warning never describe one
    conflict two different ways.
    """
    first_seen: dict[str, Occurrence] = {}
    for occurrence in sorted(occurrences, key=sort_key):
        if occurrence.default is not None and occurrence.default not in first_seen:
            first_seen[occurrence.default] = occurrence

    if len(first_seen) < 2:
        return ""

    return ", ".join(
        f"{default!r} ({occurrence.file}:{occurrence.line})"
        for default, occurrence in sorted(first_seen.items())
    )


def _conflict_warning(name: str, code_findings: list[Finding]) -> str | None:
    """Warn when call sites disagree about a variable's default."""
    cited = cite_defaults(f.occurrence for f in code_findings)
    return f"Conflicting defaults for {name}: {cited}" if cited else None


def aggregate(findings: Iterable[Finding]) -> AggregateResult:
    """Group findings by name and apply the aggregation rules.

    Accepts findings from all three sources at once; which axis each one sits
    on is read off its occurrence rather than passed in separately.
    """
    by_name: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_name[finding.name].append(finding)

    variables: list[Variable] = []
    warnings: list[str] = []

    for name, group in sorted(by_name.items()):
        code = [f for f in group if f.occurrence.source is SourceKind.CODE]

        # required and defaults come only from code. An .env.example line does
        # not make a variable required, and a compose key has no fallback.
        required = any(f.occurrence.required for f in code)
        defaults = tuple(
            sorted({f.occurrence.default for f in code if f.occurrence.default is not None})
        )

        deployment_targets = tuple(
            sorted(
                {
                    str(f.occurrence.file)
                    for f in group
                    if f.occurrence.source is SourceKind.DEPLOYMENT
                }
            )
        )

        conflict = _conflict_warning(name, code)
        if conflict is not None:
            warnings.append(conflict)

        variables.append(
            Variable(
                name=name,
                required=required,
                confidence=_best_confidence(group),
                # Placeholders. audit.py replaces both once it knows which of
                # the three axes this name appeared on.
                status=Status.OK,
                statuses=frozenset(),
                defaults=defaults,
                occurrences=tuple(sorted((f.occurrence for f in group), key=sort_key)),
                documented_in_example=any(f.occurrence.source is SourceKind.EXAMPLE for f in group),
                deployment_targets=deployment_targets,
            )
        )

    return AggregateResult(variables=tuple(variables), warnings=tuple(sorted(warnings)))
