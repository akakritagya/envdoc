"""Constructors for model objects, so tests can state only what they mean.

An Occurrence has seven fields and most tests care about one or two of them.
Spelling all seven out at every call site buries the field under test in
boilerplate and makes it easy to miss that two "identical" fixtures differ in
some incidental way.
"""

from pathlib import PurePosixPath

from envdoc.models import Confidence, Finding, Occurrence, Provider, SourceKind


def occurrence(
    file: str = "src/app.py",
    line: int = 1,
    column: int = 0,
    *,
    source: SourceKind = SourceKind.CODE,
    provider: Provider = Provider.PYTHON_AST,
    required: bool = True,
    default: str | None = None,
) -> Occurrence:
    return Occurrence(
        file=PurePosixPath(file),
        line=line,
        column=column,
        source=source,
        provider=provider,
        required=required,
        default=default,
    )


def finding(
    name: str,
    file: str = "src/app.py",
    line: int = 1,
    column: int = 0,
    *,
    source: SourceKind = SourceKind.CODE,
    provider: Provider = Provider.PYTHON_AST,
    required: bool = True,
    default: str | None = None,
    confidence: Confidence = Confidence.EXACT,
) -> Finding:
    return Finding(
        name=name,
        occurrence=occurrence(
            file,
            line,
            column,
            source=source,
            provider=provider,
            required=required,
            default=default,
        ),
        confidence=confidence,
    )


def example_entry(name: str, file: str = ".env.example", line: int = 1) -> Finding:
    """A name documented in .env.example. Never required, never has a default."""
    return finding(
        name,
        file,
        line,
        source=SourceKind.EXAMPLE,
        provider=Provider.DOTENV_EXAMPLE,
        required=False,
        default=None,
    )


def deployment_entry(name: str, file: str = "docker-compose.yml", line: int = 1) -> Finding:
    """A name a deployment manifest sets. Never required, never has a default."""
    return finding(
        name,
        file,
        line,
        source=SourceKind.DEPLOYMENT,
        provider=Provider.DOCKER_COMPOSE,
        required=False,
        default=None,
    )
