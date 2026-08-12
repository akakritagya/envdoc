"""The three-way set algebra: which axes a name appears on, and the verdict.

The module the tool is named for. Two-way scanners compare *used in code*
against *listed in .env.example* and stop there. The third axis -- what the
deployment actually provides -- catches a variable the code requires, the
example documents, and the compose file never sets. It works on the developer's
laptop and the container dies on boot, and no two-way audit can see it coming.

    code  example  deployment   verdict
    ----  -------  ----------   -------
     y      y        y          OK
     y      y        n          UNSET_IN_DEPLOYMENT
     y      n        y          UNDOCUMENTED
     y      n        n          UNDOCUMENTED + UNSET_IN_DEPLOYMENT
     n      y        y          STALE
     n      y        n          STALE
     n      n        y          ORPHAN_DEPLOYMENT

A name on none of the three produces no occurrences and therefore no Variable,
which is why there are seven rows rather than eight.

Two asymmetries in those rules are deliberate and easy to mistake for
inconsistency:

    - A missing .env.example leaves every variable UNDOCUMENTED, but missing
      deployment manifests leave nothing UNSET_IN_DEPLOYMENT. The absence of an
      example file *is* the finding -- documenting configuration is what this
      tool is for. The absence of a compose file means the project was never
      containerised, which is not a defect, and flagging every required
      variable in a library would teach people to ignore the one status that
      matters most.

    - UNSET_IN_DEPLOYMENT is reported for every variable but gates the build
      only for required ones, which is handled in Report.has_drift. A variable
      with a fallback that no manifest sets degrades to its default; one
      without takes the process down on boot.

No I/O happens here, by rule. Everything this needs -- which files were
scanned, which manifests were found -- arrives as an argument, which is what
lets the whole table be tested with plain assertions.
"""

from collections.abc import Iterable
from pathlib import PurePosixPath

from envdoc.aggregate import aggregate
from envdoc.models import DynamicRef, Finding, Report, SourceKind, Status, Variable

# Which status leads when a variable carries more than one. Ordered by
# consequence rather than by how the enum happens to be written: unset in
# deployment kills the process on boot, undocumented means nobody knows to set
# it, stale is dead documentation, orphaned is dead configuration. A table
# shows one status per row, and this decides which one that is -- the full set
# stays on the variable, because collapsing to the headline would hide half of
# what needs fixing.
_DEFAULT_ROOT = PurePosixPath(".")

_HEADLINE_ORDER = (
    Status.UNSET_IN_DEPLOYMENT,
    Status.UNDOCUMENTED,
    Status.STALE,
    Status.ORPHAN_DEPLOYMENT,
    Status.OK,
)

# _headline's next() has no default, so a Status left out of the order above
# would raise StopIteration instead of a clear failure -- and it would do so
# from inside `check`, on whichever CI run first produces that status. This
# turns the gap into an import-time assertion instead.
assert set(_HEADLINE_ORDER) == set(Status), "every Status must appear in _HEADLINE_ORDER"


def _statuses(variable: Variable, *, manifests_found: bool) -> frozenset[Status]:
    """Every status that applies to one variable, from the table above."""
    in_code = any(o.source is SourceKind.CODE for o in variable.occurrences)
    in_example = variable.documented_in_example
    in_deployment = bool(variable.deployment_targets)

    statuses: set[Status] = set()

    if in_code and not in_example:
        statuses.add(Status.UNDOCUMENTED)
    if in_example and not in_code:
        statuses.add(Status.STALE)
    if in_code and manifests_found and not in_deployment:
        statuses.add(Status.UNSET_IN_DEPLOYMENT)
    if in_deployment and not in_code and not in_example:
        # Only when nothing else explains it. A name the example documents and
        # a manifest sets is already STALE -- "nothing reads this" is the same
        # complaint, said once.
        statuses.add(Status.ORPHAN_DEPLOYMENT)

    # Never empty once audited. aggregate() uses the empty set as its
    # placeholder, and a placeholder that survives into a report is exactly the
    # kind of value that quietly becomes load-bearing.
    return frozenset(statuses) if statuses else frozenset({Status.OK})


def audit(
    findings: Iterable[Finding],
    *,
    root: PurePosixPath = _DEFAULT_ROOT,
    dynamic: Iterable[DynamicRef] = (),
    warnings: Iterable[str] = (),
    files_scanned: int = 0,
    deployment_files: Iterable[str] = (),
) -> Report:
    """Aggregate findings into variables and decide each one's status.

    `deployment_files` is the manifests that were found and parsed, not the
    ones that yielded findings. The difference is the whole point: a
    docker-compose.yml with no `environment:` block is precisely the situation
    where every variable is unset in deployment, and inferring the manifests
    from the deployment occurrences would conclude there were none and call the
    repository clean.
    """
    manifests = tuple(sorted(set(deployment_files)))
    aggregated = aggregate(findings)

    variables: list[Variable] = []
    for variable in aggregated.variables:
        statuses = _statuses(variable, manifests_found=bool(manifests))
        variables.append(
            Variable(
                name=variable.name,
                required=variable.required,
                confidence=variable.confidence,
                status=_headline(statuses),
                statuses=statuses,
                defaults=variable.defaults,
                occurrences=variable.occurrences,
                documented_in_example=variable.documented_in_example,
                deployment_targets=variable.deployment_targets,
            )
        )

    return Report(
        root=root,
        variables=tuple(variables),
        dynamic=tuple(dynamic),
        # Discovery, the parsers and aggregation all produce warnings and the
        # CLI prints one list. Sorting is what keeps that list identical run to
        # run, which the golden tests depend on.
        warnings=tuple(sorted([*warnings, *aggregated.warnings])),
        files_scanned=files_scanned,
        deployment_files_found=manifests,
    )


def _headline(statuses: frozenset[Status]) -> Status:
    """The one status to lead with, by consequence."""
    return next(status for status in _HEADLINE_ORDER if status in statuses)
