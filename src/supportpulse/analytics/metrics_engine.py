"""Support KPI aggregation built on pandas and NumPy."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from supportpulse.analytics.cycle_times import CycleTimeCalculator
from supportpulse.models.metrics import SupportMetrics
from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket, Status


class MetricsEngine:
    """Computes headline and grouped support KPIs from a ticket population."""

    def __init__(
        self,
        sla_policy: SLAPolicy | None = None,
        hours_per_day: float = 8.0,
        window_days: int | None = None,
    ) -> None:
        self.sla_policy = sla_policy or SLAPolicy.default()
        self.hours_per_day = hours_per_day
        self.window_days = window_days
        self.cycle_times = CycleTimeCalculator(hours_per_day)

    # ------------------------------------------------------------------
    # Frame construction
    # ------------------------------------------------------------------
    def to_frame(self, tickets: Iterable[JiraTicket]) -> pd.DataFrame:
        """Flatten tickets into a tidy pandas DataFrame."""

        rows = [ticket.to_ledger_row(self.hours_per_day) for ticket in tickets]
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame["created_date"] = pd.to_datetime(frame["created_at"]).dt.date
        return frame

    # ------------------------------------------------------------------
    # KPI computation
    # ------------------------------------------------------------------
    def compute(
        self, tickets: Iterable[JiraTicket], reference: datetime | None = None
    ) -> SupportMetrics:
        """Aggregate the full :class:`SupportMetrics` bundle."""

        tickets = list(tickets)
        reference = reference or datetime.now(timezone.utc)
        if not tickets:
            return SupportMetrics()

        frame = self.to_frame(tickets)
        resolved_mask = frame["resolution_hours"].notna()
        resolved = frame[resolved_mask]
        open_frame = frame[~resolved_mask]

        resolution_values = resolved["resolution_hours"].to_numpy(dtype=float)
        response_values = frame["first_response_hours"].dropna().to_numpy(dtype=float)

        window_days = self.window_days or self._window_days(frame)
        window_days = max(1, int(window_days))

        metrics = SupportMetrics(
            total_tickets=int(len(frame)),
            resolved_tickets=int(resolved_mask.sum()),
            open_tickets=int((~resolved_mask).sum()),
            wip=int((~resolved_mask).sum()),
            window_days=window_days,
            generated_at=reference,
        )

        metrics.mttr_hours = _safe_mean(resolution_values)
        metrics.mttd_hours = self._mean_time_to_detect(tickets)
        metrics.first_response_hours = _safe_mean(response_values)

        percentiles = self.cycle_times.percentiles(resolution_values, (50, 85, 95))
        metrics.p50_hours = percentiles["p50"]
        metrics.p85_hours = percentiles["p85"]
        metrics.p95_hours = percentiles["p95"]

        metrics.fcr_rate = self._first_contact_resolution(tickets)
        metrics.breach_rate = self._breach_rate(tickets)

        metrics.throughput_per_day = round(metrics.resolved_tickets / window_days, 3)
        metrics.inflow_per_day = round(metrics.total_tickets / window_days, 3)
        metrics.backlog_growth = round(
            metrics.inflow_per_day - metrics.throughput_per_day, 3
        )

        metrics.by_priority = self._by_priority(resolved)
        metrics.by_component = (
            frame["component"].value_counts().to_dict() if not frame.empty else {}
        )
        metrics.by_status = (
            frame["status"].value_counts().to_dict() if not frame.empty else {}
        )
        metrics.by_component = {str(k): int(v) for k, v in metrics.by_component.items()}
        metrics.by_status = {str(k): int(v) for k, v in metrics.by_status.items()}
        return metrics

    # ------------------------------------------------------------------
    # Component KPIs
    # ------------------------------------------------------------------
    def _window_days(self, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 1
        created = pd.to_datetime(frame["created_at"])
        span = (created.max() - created.min()).days + 1
        return max(1, int(span))

    def _mean_time_to_detect(self, tickets: list[JiraTicket]) -> float:
        values: list[float] = []
        for ticket in tickets:
            for transition in ticket.transitions:
                if transition.to_enum in {Status.IN_TRIAGE, Status.IN_PROGRESS}:
                    from supportpulse.models.ticket import business_hours_between

                    values.append(
                        business_hours_between(
                            ticket.created_at, transition.at, self.hours_per_day
                        )
                    )
                    break
        return _safe_mean(np.asarray(values, dtype=float))

    def _first_contact_resolution(self, tickets: list[JiraTicket]) -> float:
        resolved = [t for t in tickets if t.is_resolved]
        if not resolved:
            return 0.0
        fcr = 0
        waiting = {Status.WAITING_CUSTOMER, Status.WAITING_VENDOR}
        for ticket in resolved:
            touched_waiting = any(t.to_enum in waiting for t in ticket.transitions)
            if not touched_waiting and ticket.reassignment_count() == 0:
                fcr += 1
        return round(fcr / len(resolved), 4)

    def _breach_rate(self, tickets: list[JiraTicket]) -> float:
        resolved = [t for t in tickets if t.is_resolved]
        if not resolved:
            return 0.0
        breaches = 0
        for ticket in resolved:
            hours = ticket.resolution_hours(self.hours_per_day) or 0.0
            target = self.sla_policy.target_for(ticket.priority).resolution_hours
            if hours > target:
                breaches += 1
        return round(breaches / len(resolved), 4)

    def _by_priority(self, resolved: pd.DataFrame) -> dict[str, dict[str, float]]:
        if resolved.empty:
            return {}
        grouped: dict[str, dict[str, float]] = {}
        for priority, group in resolved.groupby("priority"):
            values = group["resolution_hours"].to_numpy(dtype=float)
            target = self.sla_policy.target_for(priority).resolution_hours
            breached = int((values > target).sum())
            grouped[str(priority)] = {
                "resolved": int(len(values)),
                "mttr_hours": round(_safe_mean(values), 3),
                "p85_hours": round(self.cycle_times.percentile(values, 85), 3),
                "breaches": breached,
                "breach_rate": round(breached / len(values), 4) if len(values) else 0.0,
                "sla_target_hours": round(target, 3),
            }
        return grouped

    # ------------------------------------------------------------------
    # SLA compliance helpers
    # ------------------------------------------------------------------
    def sla_compliance_table(self, tickets: Iterable[JiraTicket]) -> pd.DataFrame:
        """Per-priority SLA attainment summary for reporting."""

        tickets = list(tickets)
        rows = []
        for priority in {t.priority.value for t in tickets}:
            subset = [t for t in tickets if t.priority.value == priority]
            resolved = [t for t in subset if t.is_resolved]
            target = self.sla_policy.target_for(priority).resolution_hours
            within = [
                t for t in resolved if (t.resolution_hours(self.hours_per_day) or 0) <= target
            ]
            attainment = (len(within) / len(resolved)) if resolved else 1.0
            rows.append(
                {
                    "priority": priority,
                    "tickets": len(subset),
                    "resolved": len(resolved),
                    "open": len(subset) - len(resolved),
                    "sla_target_hours": round(target, 2),
                    "attainment": round(attainment, 4),
                    "status": _rag(attainment),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        return frame.sort_values("priority").reset_index(drop=True)

    def sla_compliance_table_from_metrics(self, metrics: SupportMetrics) -> pd.DataFrame:
        """Rebuild the per-priority attainment table from a metrics bundle."""

        rows = []
        for priority, values in metrics.by_priority.items():
            resolved = int(values.get("resolved", 0))
            breach_rate = float(values.get("breach_rate", 0.0))
            attainment = (1.0 - breach_rate) if resolved else 1.0
            rows.append(
                {
                    "priority": priority,
                    "resolved": resolved,
                    "attainment": round(attainment, 4),
                    "status": _rag(attainment),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=["priority", "resolved", "attainment", "status"])
        return frame.sort_values("priority").reset_index(drop=True)

    def component_hotspots(
        self, tickets: Iterable[JiraTicket], top_n: int = 8
    ) -> pd.DataFrame:
        """Components ranked by defect volume, breaches and MTTR."""

        tickets = list(tickets)
        rows = []
        for component in {t.component or "Unassigned" for t in tickets}:
            subset = [t for t in tickets if (t.component or "Unassigned") == component]
            resolved = [t for t in subset if t.is_resolved]
            values = np.asarray(
                [t.resolution_hours(self.hours_per_day) or 0.0 for t in resolved], dtype=float
            )
            breaches = sum(
                1
                for t in resolved
                if (t.resolution_hours(self.hours_per_day) or 0)
                > self.sla_policy.target_for(t.priority).resolution_hours
            )
            rows.append(
                {
                    "component": component,
                    "tickets": len(subset),
                    "resolved": len(resolved),
                    "mttr_hours": round(_safe_mean(values), 2),
                    "breaches": breaches,
                    "breach_rate": round(breaches / len(resolved), 3) if resolved else 0.0,
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        return frame.sort_values(
            ["breaches", "tickets"], ascending=False
        ).head(top_n).reset_index(drop=True)


def _safe_mean(values: np.ndarray) -> float:
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return 0.0
    return float(np.mean(array))


def _rag(attainment: float) -> str:
    if attainment >= 0.9:
        return "Green"
    if attainment >= 0.75:
        return "Amber"
    return "Red"
