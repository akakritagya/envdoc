"""Read `process.env` access out of JavaScript and TypeScript source, via
tree-sitter.

The second extractor family, after `python_ast.py`, and the first this
codebase cannot lean on a language's own standard-library parser for. It
plugs into the same seam that one did: `extract(text, file) -> ExtractResult`,
a resolved name becomes a `Finding`, an unresolved key becomes a `DynamicRef`,
and neither renderer nor aggregator downstream needs to know which parser
produced either.

Three node shapes carry a `process.env` read, checked directly against
`_is_process_env` rather than by matching text:

    process.env.API_KEY            member_expression
    process.env["API_KEY"]         subscript_expression
    const { API_KEY } = process.env   object_pattern

Grammar selection is the *only* place a file's extension matters. Every check
below is language-agnostic -- confirmed by feeding identical `process.env`
patterns through the JS, TS and TSX grammars before writing this module and
getting identical node shapes back, including through TypeScript's `as` casts,
`!` assertions, and JSX interpolation. A `.tsx` file's `{process.env.PORT}`
parses to the same `member_expression` a plain `.js` file's `process.env.PORT`
does.

The gate's named false-positive case needs no special handling for the same
reason a Python string literal is invisible to `ast.walk`: the literal text of
a template string is a bare `string_fragment` node. `` `process.env` `` typed
inside backticks as prose produces zero `member_expression` nodes; only a real
`${process.env.X}` interpolation does, because only that is actually parsed as
an expression.

Unlike `ast.parse`, a tree-sitter parse never raises. Malformed source still
produces a tree -- error nodes where it couldn't make sense of something, real
nodes everywhere it could -- so a garbled file quietly yields fewer or zero
findings instead of taking the scan down. No `try/except` is needed here for
the same reason `python_ast.py` needs one: tree-sitter already made syntax
errors non-fatal at the parser level.

What this module does not attempt: tracking a rebound `process` (`import
process from 'node:process'`), same simplification `python_ast.py` made before
Python got its own alias-tracking group, and with no follow-up group of its
own scheduled here. Nor does it resolve `...rest` / spread capture of the
whole `process.env` object, or any runtime's env access other than Node's
`process.env` -- see the module's build-plan entry for the full list.
"""

from pathlib import PurePosixPath

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

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

_JS_EXTENSIONS = frozenset({".js", ".mjs", ".cjs", ".jsx"})
_TSX_EXTENSIONS = frozenset({".tsx"})
# Anything else this module is registered for (.ts) is plain TypeScript.

_JS_LANGUAGE = Language(tree_sitter_javascript.language())
_TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
_TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())

_FALLBACK_OPERATORS = frozenset({"||", "??"})


def _language_for(file: PurePosixPath) -> Language:
    if file.suffix in _JS_EXTENSIONS:
        return _JS_LANGUAGE
    if file.suffix in _TSX_EXTENSIONS:
        return _TSX_LANGUAGE
    return _TS_LANGUAGE


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _is_process_env(node: Node | None) -> bool:
    """Whether `node` is structurally `process.env`, however it is reached."""
    if node is None or node.type != "member_expression":
        return False
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    return (
        obj is not None
        and obj.type == "identifier"
        and obj.text == b"process"
        and prop is not None
        and prop.type == "property_identifier"
        and prop.text == b"env"
    )


def _string_literal(node: Node | None) -> str | None:
    """The literal text of a plain (non-template) JS/TS string, if `node` is one.

    A template string is deliberately never unwrapped here even when it has no
    interpolation, the same way `python_ast.py` never unwraps an f-string with
    no substitutions -- a name resolved from syntax that merely happens to be
    static this time is still a guess about syntax that isn't a plain literal.
    """
    if node is None or node.type != "string":
        return None
    fragments = [child for child in node.children if child.type == "string_fragment"]
    return "".join(_text(fragment) for fragment in fragments)


def _read_fallback(node: Node) -> tuple[bool, str | None]:
    """Whether `node` (a process.env read) has a usable fallback, and its
    literal value if there is one.

    `node` counts as having a fallback only when it is literally the `left`
    operand (compared by identity, not text) of a parent `||`/`??`
    `binary_expression` -- mirrors `python_ast._fallback`'s two-way split for
    `os.getenv`'s second argument: a literal on the other side is
    documentable, a real but non-literal expression still makes the read
    optional but leaves nothing to write into `.env.example`.
    """
    parent = node.parent
    if parent is None or parent.type != "binary_expression":
        return (True, None)
    operator = parent.child_by_field_name("operator")
    if operator is None or operator.type not in _FALLBACK_OPERATORS:
        return (True, None)
    left = parent.child_by_field_name("left")
    if left is None or left.id != node.id:
        return (True, None)
    right = parent.child_by_field_name("right")
    literal = _string_literal(right)
    return (False, literal)


def _occurrence(
    node: Node, file: PurePosixPath, *, required: bool, default: str | None
) -> Occurrence:
    return Occurrence(
        file=file,
        line=node.start_point.row + 1,
        column=node.start_point.column,
        source=SourceKind.CODE,
        provider=Provider.TS_JS,
        required=required,
        default=default,
    )


def _destructure(pattern: Node, file: PurePosixPath) -> list[Finding]:
    """Every name a `{ ... } = process.env` pattern resolves, per child shape.

    `rest_pattern` (`...rest`) is skipped: it names no single variable, the
    same posture an unresolvable dynamic key takes except there is not even
    an expression here worth recording as a DynamicRef.
    """
    findings: list[Finding] = []

    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            name = _text(child)
            if name:
                occurrence = _occurrence(child, file, required=True, default=None)
                findings.append(
                    Finding(name=name, occurrence=occurrence, confidence=Confidence.EXACT)
                )

        elif child.type == "object_assignment_pattern":
            left = child.child_by_field_name("left")
            if left is None or left.type != "shorthand_property_identifier_pattern":
                continue
            name = _text(left)
            if not name:
                continue
            right = child.child_by_field_name("right")
            occurrence = _occurrence(child, file, required=False, default=_string_literal(right))
            findings.append(Finding(name=name, occurrence=occurrence, confidence=Confidence.EXACT))

        elif child.type == "pair_pattern":
            key = child.child_by_field_name("key")
            if key is None or key.type != "property_identifier":
                continue
            name = _text(key)
            if not name:
                continue
            value = child.child_by_field_name("value")
            if value is not None and value.type == "assignment_pattern":
                right = value.child_by_field_name("right")
                occurrence = _occurrence(
                    child, file, required=False, default=_string_literal(right)
                )
            else:
                occurrence = _occurrence(child, file, required=True, default=None)
            findings.append(Finding(name=name, occurrence=occurrence, confidence=Confidence.EXACT))

        # rest_pattern, and anything else this survey didn't encounter, is
        # deliberately skipped -- see the module docstring.

    return findings


def _destructure_source(pattern: Node) -> Node | None:
    """The expression a `{ ... } = <this>` pattern destructures, in either the
    declaration form (`const { X } = ...`) or the bare assignment form
    (`({ X } = ...)`)."""
    parent = pattern.parent
    if parent is None:
        return None
    if parent.type == "variable_declarator":
        return parent.child_by_field_name("value")
    if parent.type == "assignment_expression":
        return parent.child_by_field_name("right")
    return None


def extract(text: str, file: PurePosixPath) -> ExtractResult:
    """Find every `process.env` read in one JS/TS/JSX/TSX file.

    `file` is recorded as given and should already be relative to the scan
    root, matching `python_ast.py`'s and `dotenv.py`'s `extract` signature.
    """
    parser = Parser(_language_for(file))
    source = text.encode("utf-8")
    tree = parser.parse(source)

    findings: list[Finding] = []
    dynamic: list[DynamicRef] = []

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)

        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if not _is_process_env(obj) or prop is None or prop.type != "property_identifier":
                continue
            name = _text(prop)
            if not name:
                continue
            required, default = _read_fallback(node)
            occurrence = _occurrence(node, file, required=required, default=default)
            findings.append(Finding(name=name, occurrence=occurrence, confidence=Confidence.EXACT))

        elif node.type == "subscript_expression":
            obj = node.child_by_field_name("object")
            index = node.child_by_field_name("index")
            if not _is_process_env(obj) or index is None:
                continue
            required, default = _read_fallback(node)
            occurrence = _occurrence(node, file, required=required, default=default)
            literal = _string_literal(index)
            if literal is not None:
                if literal:
                    findings.append(
                        Finding(name=literal, occurrence=occurrence, confidence=Confidence.EXACT)
                    )
                continue
            dynamic.append(DynamicRef(occurrence=occurrence, expression=_text(index)))

        elif node.type == "object_pattern":
            source_node = _destructure_source(node)
            if _is_process_env(source_node):
                findings.extend(_destructure(node, file))

    findings.sort(key=lambda f: sort_key(f.occurrence))
    dynamic.sort(key=lambda d: sort_key(d.occurrence))

    return ExtractResult(findings=tuple(findings), dynamic=tuple(dynamic), warnings=())
