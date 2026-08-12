"""Read the variable names a `docker-compose.yml` provides at deploy time.

G8b's whole reason to exist: the flagship case envdoc is named for --
required in code, documented in `.env.example`, absent from the compose
file's `environment:` -- is invisible to a two-way audit and undetectable by
this tool until something reads a deployment manifest. This is the minimal
reader that makes it detectable. Deliberately narrow: only the `environment:`
key under `services.<name>`, in both spellings Compose accepts --

    environment:
      - PORT=8000        # list form, "KEY=value"
      - DEBUG             # list form, bare name -- passed through from the
                           # host's own environment at deploy time

    environment:
      DATABASE_URL: postgres://localhost   # mapping form, "KEY: value"
      REDIS_URL:                           # mapping form, bare name (null)

Both spellings of "bare name" still produce a Finding: envdoc's three-way
audit only asks whether a manifest *declares* a name, never what value it
resolves to, and a bare name is exactly as much a deliberate declaration as
one with a literal value. `env_file:`, `${VAR}` interpolation elsewhere in
the file, ports, volumes, and every other Compose key are out of scope here
-- G14 is where this parser gets deepened, not this one.

Parsed with `yaml.compose()` rather than `yaml.safe_load()`, and pinned to
`SafeLoader`: `compose()` only parses and builds a graph of `Node` objects,
never constructing a Python object from a YAML tag, which is what makes
`safe_load` itself safe against untrusted input. Using it directly gets that
same safety *and* a `start_mark.line` on every node -- safe_load hands back
plain dicts and lists with no position information at all, and every other
extractor in this codebase reports a real line number.
"""

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


def _mapping_value(node: yaml.Node, key: str) -> yaml.Node | None:
    """The value node for `key` in a mapping node, or None if absent or if
    `node` isn't a mapping at all -- a malformed service definition is
    skipped rather than treated as a parse failure for the whole file."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            assert isinstance(value_node, yaml.Node)
            return value_node
    return None


def _names_from_list(node: yaml.SequenceNode) -> Iterator[tuple[str, int]]:
    for item in node.value:
        if not isinstance(item, yaml.ScalarNode):
            continue
        name = item.value.split("=", 1)[0].strip()
        if name:
            yield name, item.start_mark.line + 1


def _names_from_mapping(node: yaml.MappingNode) -> Iterator[tuple[str, int]]:
    for key_node, _value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value:
            yield key_node.value, key_node.start_mark.line + 1


def _environment_names(node: yaml.Node) -> Iterator[tuple[str, int]]:
    """Every name declared under one service's `environment:`, however it was
    spelled. Anything that's neither a list nor a mapping -- `environment:
    null`, a bare string -- is malformed Compose and yields nothing rather
    than raising: one broken service shouldn't take the whole scan down."""
    if isinstance(node, yaml.SequenceNode):
        yield from _names_from_list(node)
    elif isinstance(node, yaml.MappingNode):
        yield from _names_from_mapping(node)


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Every variable name declared in any service's `environment:` block."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        return ExtractResult(
            findings=(), dynamic=(), warnings=(f"{file}: could not parse, skipped ({exc})",)
        )

    services = _mapping_value(root, "services") if root is not None else None
    if not isinstance(services, yaml.MappingNode):
        return ExtractResult(findings=(), dynamic=(), warnings=())

    findings: list[Finding] = []
    for _service_name, service in services.value:
        environment = _mapping_value(service, "environment")
        if environment is None:
            continue
        for name, line in _environment_names(environment):
            findings.append(
                Finding(
                    name=name,
                    occurrence=Occurrence(
                        file=file,
                        line=line,
                        column=0,
                        source=SourceKind.DEPLOYMENT,
                        provider=Provider.DOCKER_COMPOSE,
                        # Both code-only by the model's rule -- a compose key
                        # has no fallback and "required" describes a call
                        # site, not a manifest entry.
                        required=False,
                        default=None,
                    ),
                    confidence=Confidence.EXACT,
                )
            )

    findings.sort(key=lambda f: sort_key(f.occurrence))
    return ExtractResult(findings=tuple(findings), dynamic=(), warnings=())
