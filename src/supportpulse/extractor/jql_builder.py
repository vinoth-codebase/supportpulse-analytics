"""Programmatic JQL query construction.

Example:
    >>> JQLBuilder("SUP").updated_since(days=30).order_by("created", "DESC").build()
    'project = SUP AND updated >= -30d ORDER BY created DESC'
"""

from __future__ import annotations

from supportpulse.models.ticket import Priority, Status

_ORDER_DIRECTIONS = {"ASC", "DESC"}


class JQLBuilder:
    """Immutable-ish, chainable JQL statement builder."""

    def __init__(self, project: str = "SUP") -> None:
        self.project = project
        self._clauses: list[str] = []
        self._order_field: str | None = "created"
        self._order_direction: str = "DESC"

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def where_project(self, project: str | None = None) -> "JQLBuilder":
        key = project or self.project
        self._clauses.append(f"project = {key}")
        return self

    def updated_since(self, days: int) -> "JQLBuilder":
        if days <= 0:
            raise ValueError("days must be a positive integer")
        self._clauses.append(f"updated >= -{int(days)}d")
        return self

    def created_since(self, days: int) -> "JQLBuilder":
        if days <= 0:
            raise ValueError("days must be a positive integer")
        self._clauses.append(f"created >= -{int(days)}d")
        return self

    def status_in(self, statuses: list[Status | str]) -> "JQLBuilder":
        values = _quote_values(statuses)
        if values:
            self._clauses.append(f"status in ({values})")
        return self

    def status_not_in(self, statuses: list[Status | str]) -> "JQLBuilder":
        values = _quote_values(statuses)
        if values:
            self._clauses.append(f"status not in ({values})")
        return self

    def priority_in(self, priorities: list[Priority | str]) -> "JQLBuilder":
        values = _quote_values(priorities)
        if values:
            self._clauses.append(f"priority in ({values})")
        return self

    def component(self, name: str) -> "JQLBuilder":
        self._clauses.append(f'component = "{name}"')
        return self

    def assignee(self, name: str) -> "JQLBuilder":
        if name.lower() in {"unassigned", "empty", "none"}:
            self._clauses.append("assignee is EMPTY")
        else:
            self._clauses.append(f'assignee = "{name}"')
        return self

    def labels_in(self, labels: list[str]) -> "JQLBuilder":
        values = _quote_values(labels)
        if values:
            self._clauses.append(f"labels in ({values})")
        return self

    def text_search(self, phrase: str) -> "JQLBuilder":
        escaped = phrase.replace('"', '\\"')
        self._clauses.append(f'text ~ "{escaped}"')
        return self

    def raw(self, clause: str) -> "JQLBuilder":
        """Append a raw JQL clause verbatim."""

        self._clauses.append(clause)
        return self

    # ------------------------------------------------------------------
    # Ordering / rendering
    # ------------------------------------------------------------------
    def order_by(self, field: str, direction: str = "DESC") -> "JQLBuilder":
        direction = direction.upper()
        if direction not in _ORDER_DIRECTIONS:
            raise ValueError(f"direction must be one of {sorted(_ORDER_DIRECTIONS)}")
        self._order_field = field
        self._order_direction = direction
        return self

    def build(self) -> str:
        """Render the accumulated clauses into a JQL string."""

        clauses = list(self._clauses)
        if not any(c.startswith("project") for c in clauses):
            clauses.insert(0, f"project = {self.project}")
        jql = " AND ".join(clauses)
        if self._order_field:
            jql = f"{jql} ORDER BY {self._order_field} {self._order_direction}"
        return jql

    # Convenience builders -------------------------------------------------
    @classmethod
    def for_support_project(
        cls, project: str = "SUP", days: int = 30, open_only: bool = False
    ) -> "JQLBuilder":
        """Standard support-ops query used by the extraction pipeline."""

        builder = cls(project).where_project().updated_since(days)
        if open_only:
            builder.status_not_in([Status.RESOLVED, Status.CLOSED])
        return builder.order_by("created", "DESC")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.build()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"JQLBuilder({self.build()!r})"


def _quote_values(values: list[object]) -> str:
    rendered: list[str] = []
    for value in values:
        text = value.value if hasattr(value, "value") else str(value)
        rendered.append(f'"{text}"')
    return ", ".join(rendered)
