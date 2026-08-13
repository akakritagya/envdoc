"""Read the environment variables a GitHub Actions workflow provides, and
the secrets/variables it reads, at CI time.

The same shape `docker_compose.py`'s G14 deepening already proved: `env:`
sets a value the CI runner provides -- `SourceKind.DEPLOYMENT`, the same
relationship `environment:`/`ENV` have -- and `${{ secrets.X }}` / `${{
vars.X }}` reads a value the workflow requires from GitHub's secret or
variable store -- `SourceKind.CODE`, the same relationship `${VAR}`/`ARG`
have. An undocumented `${{ secrets.STRIPE_KEY }}` is exactly as real a
finding as an undocumented `ARG STRIPE_KEY`.

`env:` is read at three levels -- workflow (document root), job
(`jobs.<id>.env:`), and step (`jobs.<id>.steps[].env:`) -- and is *always* a
mapping in GHA, never the list form compose's `environment:` also accepts.

`secrets.*`/`vars.*` is a single regex over every scalar in the document,
not scoped to `env:` values: a secret is at least as often passed straight
through `with:` to a reusable action (`with: token: ${{ secrets.GITHUB_TOKEN
}}`) as it is assigned to an `env:` entry, and the whole-document scan finds
either for free, the same way `docker_compose.py`'s interpolation scan finds
`${VAR}` inside `image:` without being told to look there specifically. No
brace balancing is needed here the way it was for compose's `${VAR}`
scanner -- GHA expressions don't nest and have no `:-default`-style operator
to split apart, so every match is simply `required=True, default=None`.
Both the dot form (`secrets.X`) and the bracket form (`secrets['X']`) are
matched; a bare `toJSON(secrets)` -- the whole store, no single name --
matches neither and produces nothing, the same posture a bare `process.env`
or `os.environ` already takes.

No automatic allowlist exists for `GITHUB_TOKEN` or any other
automatically-provided secret. Deciding which secrets don't need documenting
is exactly the kind of implicit guess this codebase avoids elsewhere --
`--exclude` or a baseline entry already exist for that.

Parsed with `yaml.compose()` rather than `yaml.safe_load()`, pinned to
`SafeLoader`, for the same reason `docker_compose.py` is: real line numbers
on every node, and no risk of constructing a Python object from an
untrusted YAML tag.
"""

import re
from collections.abc import Iterator
from pathlib import PurePosixPath

import yaml

from envdoc.models import (
    Confidence,
    ExtractResult,
    Finding,
    Occurrence,
    Provider,
    SourceKind,
    sort_key,
)

_REFERENCE = re.compile(
    r"\b(?:secrets|vars)\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\])"
)


def _mapping_value(node: yaml.Node, key: str) -> yaml.Node | None:
    """The value node for `key` in a mapping node, or None if absent or if
    `node` isn't a mapping at all -- a malformed job/step is skipped rather
    than treated as a parse failure for the whole file."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            assert isinstance(value_node, yaml.Node)
            return value_node
    return None


def _env_names(node: yaml.Node) -> Iterator[tuple[str, int]]:
    """Every name declared in one `env:` mapping. Always a mapping in GHA --
    unlike compose's `environment:`, there's no list-form spelling."""
    if not isinstance(node, yaml.MappingNode):
        return
    for key_node, _value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value:
            yield key_node.value, key_node.start_mark.line + 1


def _walk_scalars(node: yaml.Node) -> Iterator[yaml.ScalarNode]:
    """Every scalar in the document, keys and values alike -- a secrets/vars
    reference can appear anywhere, not just under `env:`."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk_scalars(item)
    elif isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            yield from _walk_scalars(key_node)
            yield from _walk_scalars(value_node)


def _find_references(text: str) -> Iterator[str]:
    for match in _REFERENCE.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            yield name


def _finding(
    name: str, line: int, file: PurePosixPath, *, source: SourceKind, required: bool
) -> Finding:
    return Finding(
        name=name,
        occurrence=Occurrence(
            file=file,
            line=line,
            column=0,
            source=source,
            provider=Provider.GITHUB_ACTIONS,
            required=required,
            default=None,
        ),
        confidence=Confidence.EXACT,
    )


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Every `env:` entry a workflow declares, and every `secrets.*`/`vars.*`
    reference it reads."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        return ExtractResult(
            findings=(), dynamic=(), warnings=(f"{file}: could not parse, skipped ({exc})",)
        )

    findings: list[Finding] = []

    if root is not None:
        workflow_env = _mapping_value(root, "env")
        if workflow_env is not None:
            for name, line in _env_names(workflow_env):
                findings.append(
                    _finding(name, line, file, source=SourceKind.DEPLOYMENT, required=False)
                )

        jobs = _mapping_value(root, "jobs")
        if isinstance(jobs, yaml.MappingNode):
            for _job_id, job in jobs.value:
                job_env = _mapping_value(job, "env")
                if job_env is not None:
                    for name, line in _env_names(job_env):
                        findings.append(
                            _finding(name, line, file, source=SourceKind.DEPLOYMENT, required=False)
                        )

                steps = _mapping_value(job, "steps")
                if isinstance(steps, yaml.SequenceNode):
                    for step in steps.value:
                        step_env = _mapping_value(step, "env")
                        if step_env is not None:
                            for name, line in _env_names(step_env):
                                findings.append(
                                    _finding(
                                        name,
                                        line,
                                        file,
                                        source=SourceKind.DEPLOYMENT,
                                        required=False,
                                    )
                                )

        for scalar in _walk_scalars(root):
            for name in _find_references(scalar.value):
                findings.append(
                    _finding(
                        name,
                        scalar.start_mark.line + 1,
                        file,
                        source=SourceKind.CODE,
                        required=True,
                    )
                )

    findings.sort(key=lambda f: sort_key(f.occurrence))
    return ExtractResult(findings=tuple(findings), dynamic=(), warnings=())
