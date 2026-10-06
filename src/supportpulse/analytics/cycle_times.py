"""Cycle-time analytics: ECDFs, percentiles and cumulative flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket, Status


class CycleTimeCalculator:
    """Empirical cycle-time statistics used to set realistic SLOs."""

    def __init__(self, hours_per_day: float = 8.0) -> None:
        self.hours_per_day = hours_per_day

    # ------------------------------------------------------------------
    # Basic series
    # ------------------------------------------------------------------
    def resolution_series(self, tickets: Iterable[JiraTicket]) -> np.ndarray:
        """Business-hours resolution times of the resolved tickets."""

        values = [
            hours
            for hours in (t.resolution_hours(self.hours_per_day) for t in tickets)
            if hours is not None and hours > 0
        ]
        return np.asarray(sorted(values), dtype=float)

    def first_response_series(self, tickets: Iterable[JiraTicket]) -> np.ndarray:
        values = [
            hours
            for hours in (t.first_response_hours(self.hours_per_day) for t in tickets)
            if hours is not None and hours >= 0
        ]
        return np.asarray(sorted(values), dtype=float)

    # ------------------------------------------------------------------
    # Percentiles / ECDF
    # ------------------------------------------------------------------
    def percentile(self, values: np.ndarray | Iterable[float], q: float) -> float:
        """Return the *q*-th percentile, tolerating empty input."""

        array = np.asarray(list(values), dtype=float)
        if array.size == 0:
            return 0.0
        return float(np.percentile(array, q))

    def percentiles(
        self, values: np.ndarray | Iterable[float], quantiles: Iterable[float] = (50, 85, 95)
    ) -> dict[str, float]:
        return {f"p{int(q)}": self.percentile(values, q) for q in quantiles}

    def ecdf(self, values: np.ndarray | Iterable[float]) -> tuple[np.ndarray, np.ndarray]:
        """Empirical cumulative distribution function.

        Returns:
            ``(x, y)`` where ``y[i]`` is the fraction of samples ``<= x[i]``.
        """

        array = np.sort(np.asarray(list(values), dtype=float))
        if array.size == 0:
            return np.asarray([0.0]), np.asarray([0.0])
        y = np.arange(1, array.size + 1) / array.size
        return array, y

    def ecdf_at(self, values: np.ndarray | Iterable[float], point: float) -> float:
        """ECDF evaluated at a single *point* (fraction ``<= point``)."""

        array = np.asarray(list(values), dtype=float)
        if array.size == 0:
            return 0.0
        return float(np.mean(array <= point))

    def survival_at(self, values: np.ndarray | Iterable[float], point: float) -> float:
        """Empirical survival function ``P(T > point)``."""

        array = np.asarray(list(values), dtype=float)
        if array.size == 0:
            return 1.0
        return float(np.mean(array > point))

    # ------------------------------------------------------------------
    # SLO recommendations
    # ------------------------------------------------------------------
    def recommend_slos(
        self,
        tickets: Iterable[JiraTicket],
        policy: SLAPolicy | None = None,
        confidence: float = 85.0,
    ) -> dict[str, dict[str, float]]:
        """Recommend an SLO per priority from the empirical distribution.

        The recommendation is the empirical percentile at *confidence*,
        compared against the contractual SLA target so support leadership can
        see how much headroom exists.
        """

        tickets = list(tickets)
        policy = policy or SLAPolicy.default()
        recommendations: dict[str, dict[str, float]] = {}

        grouped: dict[str, list[float]] = {}
        for ticket in tickets:
            hours = ticket.resolution_hours(self.hours_per_day)
            if hours is None or hours <= 0:
                continue
            grouped.setdefault(ticket.priority.value, []).append(hours)

        for priority, values in grouped.items():
            empirical = self.percentile(values, confidence)
            target = policy.target_for(priority).resolution_hours
            recommendations[priority] = {
                "samples": float(len(values)),
                "empirical_slo_hours": round(empirical, 2),
                "sla_target_hours": round(target, 2),
                "headroom_hours": round(target - empirical, 2),
                "attainable": 1.0 if empirical <= target else 0.0,
            }
        return recommendations

    # ------------------------------------------------------------------
    # Cumulative flow diagram
    # ------------------------------------------------------------------
    def cumulative_flow(
        self,
        tickets: Iterable[JiraTicket],
        start: datetime | None = None,
        end: datetime | None = None,
        freq: str = "D",
    ) -> pd.DataFrame:
        """Build a Cumulative Flow Diagram dataset.

        Each row is a timestamp and each column a workflow status; the value
        is how many tickets sat in that status at that timestamp.
        """

        tickets = list(tickets)
        if not tickets:
            return pd.DataFrame()

        created = [t.created_at for t in tickets]
        start = start or min(created)
        end = end or datetime.now(timezone.utc)

        index = pd.date_range(start=start, end=end, freq=freq)
        statuses = [status.value for status in Status]
        frame = pd.DataFrame(0, index=index, columns=statuses)

        for stamp in index:
            counts: dict[str, int] = {status: 0 for status in statuses}
            for ticket in tickets:
                if ticket.created_at > stamp:
                    continue
                counts[self._status_at(ticket, stamp)] += 1
            for status, value in counts.items():
                frame.at[stamp, status] = value

        frame["WIP"] = frame[
            [Status.OPEN.value, Status.IN_TRIAGE.value, Status.IN_PROGRESS.value,
             Status.WAITING_CUSTOMER.value, Status.WAITING_VENDOR.value]
        ].sum(axis=1)
        frame["Done"] = frame[[Status.RESOLVED.value, Status.CLOSED.value]].sum(axis=1)
        return frame

    @staticmethod
    def _status_at(ticket: JiraTicket, moment: datetime) -> str:
        current = Status.OPEN.value
        for transition in ticket.transitions:
            if transition.to_status and transition.at <= moment:
                current = transition.to_enum.value
        if ticket.status.is_terminal and ticket.effective_resolved_at is not None:
            if moment >= ticket.effective_resolved_at:
                current = ticket.status.value
        return current

    # ------------------------------------------------------------------
    # Aging WIP
    # ------------------------------------------------------------------
    def aging_wip(
        self, tickets: Iterable[JiraTicket], reference: datetime | None = None
    ) -> pd.DataFrame:
        """Open tickets with their current age, sorted oldest first."""

        rows = []
        for ticket in tickets:
            if ticket.is_resolved:
                continue
            rows.append(
                {
                    "key": ticket.key,
                    "priority": ticket.priority.value,
                    "status": ticket.status.value,
                    "assignee": ticket.assignee or "Unassigned",
                    "component": ticket.component or "Unassigned",
                    "age_hours": round(
                        ticket.age_hours(reference, self.hours_per_day), 2
                    ),
                    "stage_dwell_hours": round(
                        ticket.current_stage_dwell_hours(reference, self.hours_per_day), 2
                    ),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        return frame.sort_values("age_hours", ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Flow rates
    # ------------------------------------------------------------------
    def inflow_outflow(
        self,
        tickets: Iterable[JiraTicket],
        window_days: int = 14,
        reference: datetime | None = None,
    ) -> pd.DataFrame:
        """Daily inflow (created) vs outflow (resolved) counts."""

        tickets = list(tickets)
        reference = reference or datetime.now(timezone.utc)
        start = reference - timedelta(days=window_days)
        index = pd.date_range(start=start.date(), end=reference.date(), freq="D")
        inflow = pd.Series(0, index=index, dtype=int)
        outflow = pd.Series(0, index=index, dtype=int)
        valid_days = set(index.date)

        for ticket in tickets:
            created_day = ticket.created_at.date()
            if created_day in valid_days:
                inflow[pd.Timestamp(created_day)] += 1
            resolved = ticket.effective_resolved_at
            if resolved is not None:
                resolved_day = resolved.date()
                if resolved_day in valid_days:
                    outflow[pd.Timestamp(resolved_day)] += 1

        frame = pd.DataFrame({"inflow": inflow.values, "outflow": outflow.values}, index=index)
        frame["net"] = frame["inflow"] - frame["outflow"]
        frame["cumulative_backlog"] = frame["net"].cumsum()
        return frame
