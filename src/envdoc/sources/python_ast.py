"""Read environment-variable access out of Python source, via `ast`.

Parsing rather than pattern-matching is the point. A regex scanner reports the
`os.getenv("SECRET")` sitting inside a comment, inside a docstring, and inside
the string literal of a test that asserts on the message -- and a report with
three phantom variables in it is a report people stop reading. The parser
cannot see any of them, for free, because they are not code.

Four read forms are recognised, and what separates them is whether the code has
something to fall back on:

    os.environ["X"]           required -- raises on a missing key
    os.getenv("X")            required -- hands back None and fails downstream
    os.getenv("X", None)      required -- the same thing written out longhand
    os.getenv("X", "8000")    optional, default "8000"

`os.environ.get` behaves as `os.getenv` throughout. Treating a fallback-less
`getenv` as required is a deliberate reading of "no fallback": the caller is
left holding None either way, and the difference between crashing at the read
and crashing two frames later is not a difference worth documenting.

A name the parser cannot resolve -- `os.getenv(key)`, an f-string, a
concatenation -- becomes a DynamicRef rather than a guess. It has no name, so
it never becomes a Variable and never counts toward drift; it is there to say
that a read exists here which no static analysis will see.

Naive on purpose. This module recognises `os.environ` and `os.getenv` reached
through the `os` module by name, and nothing else; `from os import environ`
is alias tracking, which is its own group.
"""

import ast
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


def _is_os_environ(node: ast.expr) -> bool:
    """Whether `node` is the expression `os.environ`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_call(func: ast.expr, name: str) -> bool:
    """Whether `func` is `os.<name>`, as in the callee of `os.getenv(...)`."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _fallback(node: ast.expr | None) -> tuple[bool, str | None]:
    """Whether a read has a usable fallback, and the literal value of it.

    The two halves are separate questions. `os.getenv("PORT", compute())`
    plainly has a fallback, so the variable is not required, but there is no
    literal to put in a generated .env.example and inventing one would be a
    lie. Hence (True, None) for that case.

    An explicit `None` fallback is not a fallback: it leaves the caller in
    exactly the position the no-argument form does, holding None with no way
    to proceed.
    """
    if node is None:
        return (False, None)
    if isinstance(node, ast.Constant):
        if node.value is None:
            return (False, None)
        # str() rather than repr() so that .env.example gets PORT=8000, not
        # PORT='8000'. The literal the code wrote is the literal to document.
        return (True, node.value if isinstance(node.value, str) else str(node.value))
    return (True, None)


def _argument(call: ast.Call, position: int, name: str) -> ast.expr | None:
    """One argument of a getenv-shaped call, however the caller punctuated it.

    Both parameters of `os.getenv` are ordinary named parameters, as are those
    of the `Mapping.get` that `os.environ.get` resolves to, so `key=` and
    `default=` are legal and occasionally written. Missing a variable because
    of how its call was punctuated is the kind of silent gap that makes an
    audit tool worth less than no tool.
    """
    if len(call.args) > position:
        return call.args[position]
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def extract(source: str, file: PurePosixPath) -> ExtractResult:
    """Find every environment-variable read in one Python file.

    `file` is recorded as given and should already be relative to the scan
    root: absolute paths would leak the machine's identity into output that is
    supposed to be byte-identical everywhere.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        # ValueError is what a NUL byte used to raise and SyntaxError is what
        # current versions raise for it; catching both means a binary file that
        # slipped past discovery is skipped on every supported interpreter.
        #
        # Skipping rather than failing matters: a repository is exactly the
        # kind of place a half-finished file lives, and refusing to report on
        # the other two hundred files would make the tool useless in a hook.
        return ExtractResult(
            findings=(), dynamic=(), warnings=(f"{file}: could not parse, skipped ({exc})",)
        )

    findings: list[Finding] = []
    dynamic: list[DynamicRef] = []

    for node in ast.walk(tree):
        name_node: ast.expr
        has_fallback = False
        default: str | None = None

        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name_node = node.slice
        elif isinstance(node, ast.Call) and (
            _is_os_call(node.func, "getenv")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and _is_os_environ(node.func.value)
            )
        ):
            # `os.getenv()` is a TypeError waiting to happen, but it parses,
            # and reaching for args[0] on it would take the scan down.
            argument = _argument(node, 0, "key")
            if argument is None:
                continue
            name_node = argument
            has_fallback, default = _fallback(_argument(node, 1, "default"))
        else:
            continue

        occurrence = Occurrence(
            file=file,
            line=node.lineno,
            column=node.col_offset,
            source=SourceKind.CODE,
            provider=Provider.PYTHON_AST,
            required=not has_fallback,
            default=default,
        )

        if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
            # Resolved. An empty name is resolved too -- to nothing -- so it is
            # dropped outright rather than reported as a mystery.
            if name_node.value:
                findings.append(
                    Finding(
                        name=name_node.value,
                        occurrence=occurrence,
                        confidence=Confidence.EXACT,
                    )
                )
            continue

        dynamic.append(DynamicRef(occurrence=occurrence, expression=ast.unparse(name_node)))

    # ast.walk is breadth-first, so a read nested in a function is reached
    # after a later top-level one. Sorting here rather than trusting tree order
    # is what makes the output byte-identical run to run.
    findings.sort(key=lambda f: sort_key(f.occurrence))
    dynamic.sort(key=lambda d: sort_key(d.occurrence))

    return ExtractResult(findings=tuple(findings), dynamic=tuple(dynamic), warnings=())
