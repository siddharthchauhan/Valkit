"""Jinja filters for validation documents.

Formatting in a regulated document is not cosmetic. A bound printed to two
decimal places where the target has four hides the comparison that matters; a
digest printed in full makes a table unreadable and a digest printed too short
stops identifying anything. These filters fix the conventions in one place so
every document states its numbers the same way.

All are pure functions of their inputs.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "percent",
    "proportion",
    "bound",
    "digest",
    "utc",
    "risk_badge",
    "verdict",
    "yes_no",
    "table",
    "bullets",
    "wrap",
    "sentence",
    "count_noun",
    "FILTERS",
]


def percent(value: float | None, places: int = 1) -> str:
    """Render a proportion as a percentage."""
    if value is None:
        return "not determined"
    return f"{value * 100:.{places}f}%"


def proportion(value: float | None, places: int = 4) -> str:
    """Render a proportion at the precision acceptance decisions are made at.

    Four places by default: a target of 0.9800 and a bound of 0.9799 differ by
    a hair and by a verdict, and rounding to two places would present them as
    the same number.
    """
    if value is None:
        return "not determined"
    return f"{value:.{places}f}"


def bound(value: float | None, target: float | None = None, places: int = 4) -> str:
    """Render a confidence bound against its target, with the verdict implied."""
    if value is None:
        return "not determined"
    if target is None:
        return f"{value:.{places}f}"
    relation = ">=" if value >= target else "<"
    return f"{value:.{places}f} {relation} {target:.{places}f}"


def digest(value: str | None, length: int = 12) -> str:
    """Abbreviate a hash for inline use.

    The full digest belongs in the evidence appendix, where it can be
    recomputed and compared; inline, twelve characters identifies an object
    unambiguously within any realistic package while keeping a table legible.
    """
    if not value:
        return "—"
    return value if len(value) <= length else f"{value[:length]}…"


def utc(value: str | None) -> str:
    if not value:
        return "—"
    return value


def risk_badge(value: Any) -> str:
    """Render a risk level in a form that survives plain text."""
    text = getattr(value, "value", value)
    return str(text).upper() if text else "—"


def verdict(passed: bool | None) -> str:
    if passed is None:
        return "NOT DETERMINED"
    return "PASS" if passed else "FAIL"


def yes_no(value: Any) -> str:
    if value is None:
        return "—"
    return "Yes" if value else "No"


def sentence(text: str | None) -> str:
    """Collapse whitespace and ensure the text ends with a full stop."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if collapsed and collapsed[-1] not in ".?!:":
        collapsed += "."
    return collapsed


def wrap(text: str | None, width: int = 100) -> str:
    """Collapse whitespace, for text going into a table cell."""
    if not text:
        return "—"
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


def count_noun(count: int, singular: str, plural: str | None = None) -> str:
    """Render "1 deviation" and "3 deviations" without an inline conditional."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
    """Render a list of mappings as a Markdown table.

    Returns an em dash rather than an empty table when there are no rows: an
    empty table in a validation document looks like a rendering failure, which
    invites the reader to distrust everything around it.
    """
    rows = list(rows)
    if not rows:
        return "—"
    keys = list(columns) if columns else list(rows[0].keys())
    lines = [
        "| " + " | ".join(str(key).replace("_", " ").capitalize() for key in keys) + " |",
        "| " + " | ".join("---" for _ in keys) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(key)) for key in keys) + " |")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "—"
    text = str(value)
    # A pipe inside a cell would break the table structure.
    return text.replace("|", "\\|").replace("\n", " ")


def bullets(items: Iterable[Any], empty: str = "None.") -> str:
    """Render an iterable as a Markdown list, naming the empty case explicitly."""
    values = [str(item) for item in items if str(item).strip()]
    if not values:
        return empty
    return "\n".join(f"- {value}" for value in values)


FILTERS = {
    "percent": percent,
    "proportion": proportion,
    "bound": bound,
    "digest": digest,
    "utc": utc,
    "risk_badge": risk_badge,
    "verdict": verdict,
    "yes_no": yes_no,
    "table": table,
    "bullets": bullets,
    "wrap": wrap,
    "sentence": sentence,
    "count_noun": count_noun,
}
