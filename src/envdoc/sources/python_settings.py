"""Read `pydantic-settings` field declarations out of Python source, via `ast`.

The second thing that can read a `.py` file, alongside `python_ast.py` -- but
a genuinely different shape of analysis. Every other extractor recognises a
*call site*: `os.getenv("X")`, `process.env.X`. A `class Settings(BaseSettings)`
declares its variables as typed fields instead, and the actual environment
variable name for a field is *computed*: `env_prefix + FIELD_NAME.upper()`,
unless a per-field `alias`/`validation_alias` overrides that computation
entirely and verbatim. A regex or call-matching scanner cannot see this --
it either misses every field, or reports the bare field name, which is
simply wrong the moment `env_prefix` is set.

Those resolution rules were verified by running real `pydantic-settings`
code, not assumed from documentation:

    no alias, no prefix          FIELD_NAME.upper()
    no alias, with prefix        env_prefix + FIELD_NAME.upper()
    alias="X" / validation_alias="X"   "X", verbatim -- prefix NOT applied
    validation_alias=AliasChoices("A", "B")   both "A" and "B" are valid input

`Field(default=...)` and a bare literal default are optional and
documentable; `Field(default_factory=...)` is optional but has nothing
literal to write into `.env.example` -- the same two-way split
`python_ast._fallback` already makes for a non-constant `os.getenv` default.
`Field(...)` (a positional `Ellipsis`), or no default at all, is required.

The one correctness trap this module is built to avoid: if `env_prefix`
cannot be resolved to a literal string -- a nested legacy `class Config:
env_prefix = ...` (pydantic v1's config style, not parsed here), or a
`model_config` whose `env_prefix` is a variable -- silently falling back to
"no prefix" would not just miss a finding, it would report a name that is
not the real environment variable. The whole class is skipped instead, with
a warning naming it, the same "never fabricate" discipline `DynamicRef`
already embodies for an unresolvable call argument elsewhere in this
codebase.

As with every alias resolved anywhere in this codebase: a bare `Field(...)`
or `BaseSettings` is only trusted if this file actually imported it from
`pydantic`/`pydantic_settings`. `environs` and `django-environ` are not
covered here -- checked directly against their real APIs, both are
call-based (`env.str("NAME", default)`), structurally closer to `os.getenv`
than to a schema, and are a smaller, separate follow-up.
"""

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from envdoc.models import (
    Confidence,
    DynamicRef,
    ExtractResult,
    Finding,
    Occurrence,
    Provider,
    SourceKind,
    sort_key,
)

# Every name this module resolves, and which single module each is imported
# from in a correct pydantic-settings v2 codebase. BaseSettings moved out of
# `pydantic` entirely in v2 -- importing it from `pydantic` now raises at
# runtime in the library itself, so that path is deliberately not tracked.
_TARGET_MODULES: dict[str, str] = {
    "BaseSettings": "pydantic_settings",
    "SettingsConfigDict": "pydantic_settings",
    "Field": "pydantic",
    "AliasChoices": "pydantic",
}


@dataclass(slots=True)
class _Aliases:
    """Every local name this file bound to one of `_TARGET_MODULES`' names,
    or to one of the two modules themselves (for `module.Name` access)."""

    names: dict[str, set[str]] = field(
        default_factory=lambda: {name: set() for name in _TARGET_MODULES}
    )
    modules: dict[str, set[str]] = field(
        default_factory=lambda: {"pydantic": set(), "pydantic_settings": set()}
    )


def _collect_aliases(tree: ast.AST) -> _Aliases:
    aliases = _Aliases()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in aliases.modules:
                    aliases.modules[alias.name].add(alias.asname or alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in aliases.modules:
                continue
            for alias in node.names:
                target_module = _TARGET_MODULES.get(alias.name)
                if target_module == node.module:
                    aliases.names[alias.name].add(alias.asname or alias.name)

    return aliases


def _resolves_to(node: ast.expr | None, target: str, aliases: _Aliases) -> bool:
    """Whether `node` is `target`, however it was imported."""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in aliases.names[target]
    if isinstance(node, ast.Attribute):
        module = aliases.modules[_TARGET_MODULES[target]]
        return node.attr == target and isinstance(node.value, ast.Name) and node.value.id in module
    return False


def _is_class_var(annotation: ast.expr) -> bool:
    """Whether `annotation` is `ClassVar[...]` -- pydantic itself excludes
    these from being fields, so treating one as a variable would be a plain
    false positive."""
    if not isinstance(annotation, ast.Subscript):
        return False
    value = annotation.value
    if isinstance(value, ast.Name):
        return value.id == "ClassVar"
    return isinstance(value, ast.Attribute) and value.attr == "ClassVar"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _argument(call: ast.Call, position: int, name: str) -> ast.expr | None:
    """One argument of a call, however the caller punctuated it -- mirrors
    `python_ast._argument`, duplicated rather than imported so this module
    stays self-contained the way `docker_compose.py` and `dotenv.py` do."""
    if len(call.args) > position:
        return call.args[position]
    return _keyword(call, name)


def _literal_default(node: ast.expr) -> tuple[bool, str | None]:
    """(required, default) for a plain expression -- mirrors
    `python_ast._fallback`'s literal-vs-non-literal split. An explicit
    `None` or `Field(...)`'s positional `Ellipsis` both mean "no usable
    fallback", the same reading `_fallback` gives an explicit `None`
    `os.getenv` default."""
    if isinstance(node, ast.Constant):
        if node.value is None or node.value is Ellipsis:
            return (True, None)
        return (False, node.value if isinstance(node.value, str) else str(node.value))
    return (False, None)


def _field_call_default(call: ast.Call) -> tuple[bool, str | None]:
    default_arg = _argument(call, 0, "default")
    if default_arg is not None:
        return _literal_default(default_arg)
    if _keyword(call, "default_factory") is not None:
        return (False, None)
    return (True, None)


def _field_default(value: ast.expr | None, aliases: _Aliases) -> tuple[bool, str | None]:
    if value is None:
        return (True, None)
    if isinstance(value, ast.Call) and _resolves_to(value.func, "Field", aliases):
        return _field_call_default(value)
    return _literal_default(value)


def _resolve_alias(
    call: ast.Call, aliases: _Aliases
) -> tuple[tuple[str, ...], ast.expr | None] | None:
    """The literal names a `Field(...)` call's alias resolves to.

    `None` means no `alias`/`validation_alias` keyword was given at all --
    the caller falls through to `env_prefix + field_name.upper()`.
    Otherwise: every literal string is a name genuinely valid as input
    (`AliasChoices` contributes one per argument); the second element is set
    instead of a resolved name list when the alias value -- or any single
    choice inside an `AliasChoices` -- isn't a literal, so the caller can
    report a `DynamicRef` rather than guess.
    """
    for keyword_name in ("validation_alias", "alias"):
        node = _keyword(call, keyword_name)
        if node is None:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return ((node.value,), None)
        if isinstance(node, ast.Call) and _resolves_to(node.func, "AliasChoices", aliases):
            choices = tuple(
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
            if choices and len(choices) == len(node.args):
                return (choices, None)
        return ((), node)
    return None


def _env_prefix(class_node: ast.ClassDef) -> str | None:
    """The class's `env_prefix`, or `None` if it cannot be resolved to a
    literal -- see the module docstring for why `None` means "skip the
    whole class" rather than "assume no prefix"."""
    has_legacy_config = any(
        isinstance(item, ast.ClassDef) and item.name == "Config" for item in class_node.body
    )
    if has_legacy_config:
        return None

    model_config_value: ast.expr | None = None
    for item in class_node.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "model_config" for target in item.targets
        ):
            model_config_value = item.value
            break

    if model_config_value is None:
        return ""

    prefix_value: ast.expr | None = None
    if isinstance(model_config_value, ast.Call):
        prefix_value = _keyword(model_config_value, "env_prefix")
    elif isinstance(model_config_value, ast.Dict):
        for key, value in zip(model_config_value.keys, model_config_value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "env_prefix":
                prefix_value = value
                break
    else:
        return None

    if prefix_value is None:
        return ""
    if isinstance(prefix_value, ast.Constant) and isinstance(prefix_value.value, str):
        return prefix_value.value
    return None


def _occurrence(
    node: ast.AnnAssign, file: PurePosixPath, *, required: bool, default: str | None
) -> Occurrence:
    return Occurrence(
        file=file,
        line=node.lineno,
        column=node.col_offset,
        source=SourceKind.CODE,
        provider=Provider.PYTHON_SETTINGS,
        required=required,
        default=default,
    )


def _fields(
    class_node: ast.ClassDef, prefix: str, file: PurePosixPath, aliases: _Aliases
) -> tuple[list[Finding], list[DynamicRef]]:
    findings: list[Finding] = []
    dynamic: list[DynamicRef] = []

    for item in class_node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        if _is_class_var(item.annotation):
            continue

        value = item.value
        required, default = _field_default(value, aliases)

        alias_result = None
        if isinstance(value, ast.Call) and _resolves_to(value.func, "Field", aliases):
            alias_result = _resolve_alias(value, aliases)

        if alias_result is None:
            names: tuple[str, ...] = (f"{prefix}{item.target.id.upper()}",)
            unresolved = None
        else:
            names, unresolved = alias_result

        occurrence = _occurrence(item, file, required=required, default=default)

        if unresolved is not None:
            dynamic.append(DynamicRef(occurrence=occurrence, expression=ast.unparse(unresolved)))
            continue

        for name in names:
            findings.append(Finding(name=name, occurrence=occurrence, confidence=Confidence.EXACT))

    return findings, dynamic


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Find every `pydantic-settings` field declaration in one Python file.

    `file` is recorded as given and should already be relative to the scan
    root, matching every other extractor's `extract(text, file)` signature.
    A file that fails to parse produces an empty result rather than a
    warning of its own -- `python_ast.py`, reading the same file, already
    reports that.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ExtractResult(findings=(), dynamic=(), warnings=())

    aliases = _collect_aliases(tree)

    findings: list[Finding] = []
    dynamic: list[DynamicRef] = []
    warnings: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_resolves_to(base, "BaseSettings", aliases) for base in node.bases):
            continue

        prefix = _env_prefix(node)
        if prefix is None:
            warnings.append(
                f"{file}: Settings class '{node.name}' uses config this group cannot "
                "resolve, skipped"
            )
            continue

        class_findings, class_dynamic = _fields(node, prefix, file, aliases)
        findings.extend(class_findings)
        dynamic.extend(class_dynamic)

    findings.sort(key=lambda f: sort_key(f.occurrence))
    dynamic.sort(key=lambda d: sort_key(d.occurrence))

    return ExtractResult(
        findings=tuple(findings), dynamic=tuple(dynamic), warnings=tuple(sorted(warnings))
    )
