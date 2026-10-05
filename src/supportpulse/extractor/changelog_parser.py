"""Changelog parsing: reconstruct workflow transitions and dwell times."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from supportpulse.models.ticket import JiraTicket, Status, Transition

_TIER_HINTS = ("tier", "development", "dev", "support", "l2", "l1", "l3")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Jira sometimes emits "+0000" without a colon.
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _looks_like_tier(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(hint in lowered for hint in _TIER_HINTS)


class ChangelogParser:
    """Reconstructs state transitions from raw Jira changelog payloads."""

    def __init__(self, ping_pong_threshold: int = 3) -> None:
        self.ping_pong_threshold = ping_pong_threshold

    # ------------------------------------------------------------------
    # Core parsing
    # ------------------------------------------------------------------
    def parse_histories(self, histories: Iterable[dict[str, Any]]) -> list[Transition]:
        """Convert raw changelog ``histories`` into ordered transitions."""

        transitions: list[Transition] = []
        for history in histories or []:
            at = _parse_datetime(history.get("created"))
            if at is None:
                continue
            author = (history.get("author") or {}).get("displayName")
            tier: str | None = None
            status_change: tuple[str, str] | None = None

            for item in history.get("items", []) or []:
                field = (item.get("field") or "").lower()
                to_value = item.get("toString")
                from_value = item.get("fromString") or ""
                if field == "status":
                    status_change = (from_value, to_value or "")
                elif field in {"assignee", "assignee tier"} and _looks_like_tier(to_value):
                    tier = to_value

            if status_change is not None:
                from_status, to_status = status_change
                transitions.append(
                    Transition(
                        from_status=from_status,
                        to_status=to_status,
                        at=at,
                        author=author,
                        tier=tier,
                    )
                )
            elif tier is not None:
                # Tier-only reassignment: record as a self-transition so
                # ping-pong detection still sees the hand-off.
                transitions.append(
                    Transition(
                        from_status="",
                        to_status="",
                        at=at,
                        author=author,
                        tier=tier,
                    )
                )

        transitions.sort(key=lambda t: t.at)
        return transitions

    def parse_issue(self, raw_issue: dict[str, Any]) -> list[Transition]:
        """Parse the changelog attached to a raw Jira issue payload."""

        changelog = raw_issue.get("changelog") or {}
        histories = changelog.get("histories", []) if isinstance(changelog, dict) else []
        return self.parse_histories(histories)

    # ------------------------------------------------------------------
    # Analytics helpers
    # ------------------------------------------------------------------
    def dwell_analysis(self, ticket: JiraTicket, hours_per_day: float = 8.0) -> dict[str, Any]:
        """Return dwell-time statistics for a single ticket."""

        dwell = ticket.stage_dwell_hours(hours_per_day)
        total = sum(dwell.values()) or 1.0
        return {
            "key": ticket.key,
            "dwell_hours": dwell,
            "total_hours": round(total, 3),
            "dominant_stage": max(dwell, key=dwell.get) if dwell else None,
            "dominant_share": round(max(dwell.values()) / total, 3) if dwell else 0.0,
            "reassignments": ticket.reassignment_count(),
            "is_ping_pong": ticket.is_ping_pong(self.ping_pong_threshold),
        }

    def ping_pong_tickets(self, tickets: Iterable[JiraTicket]) -> list[JiraTicket]:
        """Filter tickets that bounce between tiers excessively."""

        return [t for t in tickets if t.is_ping_pong(self.ping_pong_threshold)]

    def stage_bottlenecks(
        self, tickets: Iterable[JiraTicket], hours_per_day: float = 8.0
    ) -> dict[str, float]:
        """Average dwell time per stage across a ticket population."""

        buckets: dict[str, list[float]] = {}
        for ticket in tickets:
            for stage, hours in ticket.stage_dwell_hours(hours_per_day).items():
                buckets.setdefault(stage, []).append(hours)
        return {
            stage: round(sum(values) / len(values), 3)
            for stage, values in buckets.items()
            if values
        }

    def transition_matrix(self, tickets: Iterable[JiraTicket]) -> dict[str, Counter]:
        """Count observed transitions per source status."""

        matrix: dict[str, Counter] = {}
        for ticket in tickets:
            for transition in ticket.transitions:
                if not transition.to_status:
                    continue
                matrix.setdefault(transition.from_status or Status.OPEN.value, Counter())[
                    transition.to_status
                ] += 1
        return matrix

    def lifecycle_path(self, ticket: JiraTicket) -> list[str]:
        """Reconstruct the human-readable lifecycle path of a ticket."""

        path: list[str] = [Status.OPEN.value]
        for transition in ticket.transitions:
            if transition.to_status:
                path.append(transition.to_status)
        if ticket.status.value not in path:
            path.append(ticket.status.value)
        # Collapse consecutive duplicates.
        collapsed: list[str] = []
        for stage in path:
            if not collapsed or collapsed[-1] != stage:
                collapsed.append(stage)
        return collapsed
