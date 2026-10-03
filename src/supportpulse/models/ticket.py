"""Jira ticket, priority, status and changelog-transition models."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Priority(str, Enum):
    """Support ticket priority ladder."""

    P1 = "P1-Critical"
    P2 = "P2-High"
    P3 = "P3-Medium"
    P4 = "P4-Low"

    @classmethod
    def from_any(cls, value: object) -> "Priority":
        """Best-effort coercion from Jira's assorted priority spellings."""

        if isinstance(value, Priority):
            return value
        text = str(value or "").strip()
        aliases = {
            "P1": cls.P1,
            "P1-CRITICAL": cls.P1,
            "CRITICAL": cls.P1,
            "BLOCKER": cls.P1,
            "HIGHEST": cls.P1,
            "P2": cls.P2,
            "P2-HIGH": cls.P2,
            "HIGH": cls.P2,
            "MAJOR": cls.P2,
            "P3": cls.P3,
            "P3-MEDIUM": cls.P3,
            "MEDIUM": cls.P3,
            "MINOR": cls.P3,
            "P4": cls.P4,
            "P4-LOW": cls.P4,
            "LOW": cls.P4,
            "LOWEST": cls.P4,
            "TRIVIAL": cls.P4,
        }
        return aliases.get(text.upper(), cls.P3)


class Status(str, Enum):
    """Support workflow statuses."""

    OPEN = "Open"
    IN_TRIAGE = "In Triage"
    IN_PROGRESS = "In Progress"
    WAITING_CUSTOMER = "Waiting on Customer"
    WAITING_VENDOR = "Waiting on Vendor"
    RESOLVED = "Resolved"
    CLOSED = "Closed"

    @property
    def is_terminal(self) -> bool:
        return self in {Status.RESOLVED, Status.CLOSED}

    @classmethod
    def from_any(cls, value: object) -> "Status":
        if isinstance(value, Status):
            return value
        text = str(value or "").strip()
        for member in cls:
            if member.value.lower() == text.lower():
                return member
        normalised = text.lower().replace("_", " ").replace("-", " ")
        aliases = {
            "to do": cls.OPEN,
            "open": cls.OPEN,
            "backlog": cls.OPEN,
            "triage": cls.IN_TRIAGE,
            "in triage": cls.IN_TRIAGE,
            "in progress": cls.IN_PROGRESS,
            "in review": cls.IN_PROGRESS,
            "waiting for customer": cls.WAITING_CUSTOMER,
            "waiting on customer": cls.WAITING_CUSTOMER,
            "pending customer": cls.WAITING_CUSTOMER,
            "waiting for vendor": cls.WAITING_VENDOR,
            "waiting on vendor": cls.WAITING_VENDOR,
            "pending vendor": cls.WAITING_VENDOR,
            "resolved": cls.RESOLVED,
            "done": cls.RESOLVED,
            "closed": cls.CLOSED,
        }
        return aliases.get(normalised, cls.OPEN)


def business_hours_between(
    start: datetime | None,
    end: datetime | None,
    hours_per_day: float = 8.0,
    business_days: Iterable[int] = (0, 1, 2, 3, 4),
    day_start_hour: int = 9,
) -> float:
    """Return the number of business hours between *start* and *end*.

    Only hours falling inside the daily business window on the configured
    business weekdays are counted. Naive datetimes are treated as-is; aware
    datetimes keep their tzinfo throughout the computation.
    """

    if start is None or end is None:
        return 0.0
    if end <= start:
        return 0.0

    days = set(business_days)
    total_seconds = 0.0
    cursor: date = start.date()
    last: date = end.date()
    tzinfo = start.tzinfo

    while cursor <= last:
        if cursor.weekday() in days:
            window_start = datetime.combine(cursor, time(day_start_hour, 0), tzinfo=tzinfo)
            window_end = window_start + timedelta(hours=hours_per_day)
            lower = max(start, window_start)
            upper = min(end, window_end)
            if upper > lower:
                total_seconds += (upper - lower).total_seconds()
        cursor += timedelta(days=1)

    return round(total_seconds / 3600.0, 4)


class Transition(BaseModel):
    """A single workflow state change parsed from the Jira changelog."""

    model_config = ConfigDict(populate_by_name=True)

    from_status: str = Field(default="")
    to_status: str
    at: datetime
    author: str | None = None
    tier: str | None = None

    @property
    def to_enum(self) -> Status:
        return Status.from_any(self.to_status)

    @property
    def from_enum(self) -> Status:
        return Status.from_any(self.from_status)


class JiraTicket(BaseModel):
    """Normalised representation of a Jira support issue."""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    summary: str = ""
    priority: Priority = Priority.P3
    status: Status = Status.OPEN
    created_at: datetime
    resolved_at: datetime | None = None
    assignee: str | None = None
    component: str | None = None
    labels: list[str] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    story_points: float | None = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority(cls, value: object) -> Priority:
        return Priority.from_any(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> Status:
        return Status.from_any(value)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None or self.status.is_terminal

    @property
    def effective_resolved_at(self) -> datetime | None:
        if self.resolved_at is not None:
            return self.resolved_at
        for transition in reversed(self.transitions):
            if transition.to_enum in {Status.RESOLVED, Status.CLOSED}:
                return transition.at
        return None

    def resolution_hours(self, hours_per_day: float = 8.0) -> float | None:
        """Business-hours resolution time, or ``None`` when still open."""

        resolved = self.effective_resolved_at
        if resolved is None:
            return None
        return business_hours_between(self.created_at, resolved, hours_per_day)

    def age_hours(
        self, reference: datetime | None = None, hours_per_day: float = 8.0
    ) -> float:
        """Business hours the ticket has been alive as of *reference*."""

        end = reference or self.effective_resolved_at or datetime.now(self.created_at.tzinfo)
        return business_hours_between(self.created_at, end, hours_per_day)

    def first_response_hours(self, hours_per_day: float = 8.0) -> float | None:
        """Business hours until the first non-Open transition, if any."""

        for transition in self.transitions:
            if transition.to_enum != Status.OPEN:
                return business_hours_between(self.created_at, transition.at, hours_per_day)
        return None

    def stage_dwell_hours(self, hours_per_day: float = 8.0) -> dict[str, float]:
        """Dwell time (business hours) spent in each visited status."""

        dwell: dict[str, float] = {}
        if not self.transitions:
            end = self.effective_resolved_at or datetime.now(self.created_at.tzinfo)
            dwell[self.status.value] = business_hours_between(
                self.created_at, end, hours_per_day
            )
            return dwell

        ordered = sorted(self.transitions, key=lambda t: t.at)
        current_status = ordered[0].from_enum.value or Status.OPEN.value
        cursor = self.created_at
        for transition in ordered:
            status_name = transition.from_enum.value if transition.from_status else current_status
            dwell[status_name] = dwell.get(status_name, 0.0) + business_hours_between(
                cursor, transition.at, hours_per_day
            )
            cursor = transition.at
            current_status = transition.to_enum.value

        end = self.effective_resolved_at or datetime.now(self.created_at.tzinfo)
        dwell[current_status] = dwell.get(current_status, 0.0) + business_hours_between(
            cursor, end, hours_per_day
        )
        return {k: round(v, 4) for k, v in dwell.items()}

    def current_stage_dwell_hours(
        self, reference: datetime | None = None, hours_per_day: float = 8.0
    ) -> float:
        """Business hours spent in the current status so far."""

        entered = self.created_at
        for transition in self.transitions:
            if transition.to_enum == self.status:
                entered = transition.at
        end = reference or self.effective_resolved_at or datetime.now(self.created_at.tzinfo)
        return business_hours_between(entered, end, hours_per_day)

    def reassignment_count(self) -> int:
        """Number of distinct tier hand-offs observed in the changelog."""

        tiers = [t.tier for t in self.transitions if t.tier]
        return sum(1 for a, b in zip(tiers, tiers[1:]) if a != b)

    def is_ping_pong(self, threshold: int = 3) -> bool:
        """True when the ticket bounced between tiers at least *threshold* times."""

        return self.reassignment_count() >= threshold

    def to_ledger_row(self, hours_per_day: float = 8.0) -> dict[str, object]:
        """Flatten the ticket into a row suitable for tabular reporting."""

        resolution = self.resolution_hours(hours_per_day)
        return {
            "key": self.key,
            "summary": self.summary,
            "priority": self.priority.value,
            "status": self.status.value,
            "component": self.component or "Unassigned",
            "assignee": self.assignee or "Unassigned",
            "created_at": self.created_at,
            "resolved_at": self.effective_resolved_at,
            "resolution_hours": resolution,
            "age_hours": round(self.age_hours(hours_per_day=hours_per_day), 2),
            "first_response_hours": self.first_response_hours(hours_per_day),
            "reassignments": self.reassignment_count(),
            "is_resolved": self.is_resolved,
        }
