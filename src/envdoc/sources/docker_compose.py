"""Read the variable names a `docker-compose.yml` provides, and the ones it
consumes, at deploy time.

G8b's minimal slice read only `services.<name>.environment`, in both
spellings Compose accepts -- list (`KEY=value`, bare `KEY`) and mapping
(`KEY: value`, bare `KEY:`). That is still exactly what it reads for names
Compose *provides*: `SourceKind.DEPLOYMENT`.

This module also reads two more things, both consumed rather than provided:

    env_file: .env.production        -- points at a sibling file; warned
                                         about, not resolved (see below)

    image: "myapp:${TAG:-latest}"    -- Compose's own variable substitution,
                                         which runs over *every* scalar value
                                         in the file, not just `environment:`

`${VAR}` interpolation is `SourceKind.CODE`, not `DEPLOYMENT` -- the compose
file *reads* `TAG` from the host at `docker compose up` time, the same
relationship `os.getenv` or `process.env` has to a variable, not the one
`environment:` has. An undocumented `${STRIPE_KEY}` is exactly as real a
finding as an undocumented `os.getenv("STRIPE_KEY")`.

`env_file:` is not resolved: doing that for real means reading a *second*
file's content, and every extractor in this codebase is `extract(text, file)`
-- single file, no I/O, no visibility into siblings. Resolving it would be a
real architecture change, not a small addition, so instead each reference is
named in a warning and left there.

Parsed with `yaml.compose()` rather than `yaml.safe_load()`, and pinned to
`SafeLoader`: `compose()` only parses and builds a graph of `Node` objects,
never constructing a Python object from a YAML tag, which is what makes
`safe_load` itself safe against untrusted input. Using it directly gets that
same safety *and* a `start_mark.line` on every node -- safe_load hands back
plain dicts and lists with no position information at all, and every other
extractor in this codebase reports a real line number.
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

_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Checked longest-and-most-specific first: ":-"/"-" are the default-value
# forms, ":?"/"?" are the required-with-message forms, and a literal ":-"
# would otherwise also satisfy a bare "-" search at the wrong position.
_OPERATORS = (":-", ":?", "-", "?")


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


def _env_file_paths(node: yaml.Node) -> Iterator[str]:
    """Every path an `env_file:` key names, bare string or list form."""
    if isinstance(node, yaml.ScalarNode):
        if node.value:
            yield node.value
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _env_file_paths(item)


def _walk_scalars(node: yaml.Node) -> Iterator[yaml.ScalarNode]:
    """Every scalar in the document, keys and values alike -- interpolation
    can appear in any of them, not just under `services.*.environment`."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk_scalars(item)
    elif isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            yield from _walk_scalars(key_node)
            yield from _walk_scalars(value_node)


def _parse_braced(inner: str) -> tuple[str, bool, str | None]:
    """(name, required, default) for the text inside one `${...}`, or an
    empty name when `inner` isn't a simple `VAR` or `VAR<operator>rest` shape
    -- a name this codebase never fabricates is one it drops instead."""
    for operator in _OPERATORS:
        index = inner.find(operator)
        if index == -1:
            continue
        name = inner[:index]
        if not _VAR_NAME.fullmatch(name):
            return "", True, None
        rest = inner[index + len(operator) :]
        if operator in (":-", "-"):
            # A default containing a further "$" is a real fallback, not a
            # literal -- nothing here is fabricated the way python_ast.py
            # never invents a literal for a non-constant os.getenv default.
            default = rest if "$" not in rest else None
            return name, False, default
        return name, True, None  # ":?" / "?": rest is an error message, never a default

    if _VAR_NAME.fullmatch(inner):
        return inner, True, None
    return "", True, None


def _find_interpolations(text: str) -> Iterator[tuple[str, bool, str | None]]:
    """Every `$VAR` / `${VAR...}` reference in `text`, left to right.

    A single scanner rather than a regex, because `${...}` needs real brace
    balancing to find its own close rather than the next unrelated `}` in
    the string. `$$` is Compose's own escaped, literal dollar sign and is
    never a reference. Interpolation nested inside another reference's
    default or error text is deliberately not sub-parsed -- see the module
    docstring's build-plan link for why.
    """
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "$":
            index += 1
            continue

        if text[index : index + 2] == "$$":
            index += 2
            continue

        if text[index : index + 2] == "${":
            depth = 1
            cursor = index + 2
            while cursor < length and depth > 0:
                if text[cursor] == "{":
                    depth += 1
                elif text[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth != 0:
                break  # unterminated -- nothing further in this scalar is trustworthy
            name, required, default = _parse_braced(text[index + 2 : cursor - 1])
            if name:
                yield name, required, default
            index = cursor
            continue

        match = _VAR_NAME.match(text, index + 1)
        if match:
            yield match.group(0), True, None
            index = match.end()
        else:
            index += 1


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Every variable name a compose file declares, and every one it reads."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        return ExtractResult(
            findings=(), dynamic=(), warnings=(f"{file}: could not parse, skipped ({exc})",)
        )

    findings: list[Finding] = []
    warnings: list[str] = []

    services = _mapping_value(root, "services") if root is not None else None
    if isinstance(services, yaml.MappingNode):
        for _service_name, service in services.value:
            environment = _mapping_value(service, "environment")
            if environment is not None:
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
                                # Both code-only by the model's rule -- a
                                # compose key has no fallback and "required"
                                # describes a call site, not a manifest entry.
                                required=False,
                                default=None,
                            ),
                            confidence=Confidence.EXACT,
                        )
                    )

            env_file = _mapping_value(service, "env_file")
            if env_file is not None:
                for path in _env_file_paths(env_file):
                    warnings.append(f"{file}: env_file: {path} referenced, not resolved")

    if root is not None:
        for scalar in _walk_scalars(root):
            for name, required, default in _find_interpolations(scalar.value):
                findings.append(
                    Finding(
                        name=name,
                        occurrence=Occurrence(
                            file=file,
                            line=scalar.start_mark.line + 1,
                            column=0,
                            source=SourceKind.CODE,
                            provider=Provider.DOCKER_COMPOSE,
                            required=required,
                            default=default,
                        ),
                        confidence=Confidence.EXACT,
                    )
                )

    findings.sort(key=lambda f: sort_key(f.occurrence))
    return ExtractResult(findings=tuple(findings), dynamic=(), warnings=tuple(sorted(warnings)))
