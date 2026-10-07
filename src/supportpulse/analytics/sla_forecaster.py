"""Statistical SLA breach-risk forecasting for active support backlogs.

The forecaster learns the empirical resolution-time distribution per priority
from historical (resolved) tickets, then scores every *active* ticket with the
conditional probability that it will still be open once its SLA resolution
target elapses:

    P(breach | survived to now) = S(target) / S(elapsed)

where ``S`` is the empirical survival function of historical resolution times.
An auxiliary stage-dwell signal captures tickets stalling in the workflow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from supportpulse.config import ForecastConfig, load_config
from supportpulse.models.metrics import BreachRiskPrediction, RiskLevel
from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket


class SLAForecaster:
    """Predicts which active tickets are about to breach their SLA."""

    def __init__(
        self,
        sla_policy: SLAPolicy | None = None,
        hours_per_day: float = 8.0,
        config: ForecastConfig | None = None,
    ) -> None:
        self.policy = sla_policy or SLAPolicy.default()
        self.hours_per_day = hours_per_day
        self.config = config or load_config().forecasting
        self.min_history = self.config.minimum_history_samples
        self.imminent_threshold = self.config.imminent_risk_threshold
        self.critical_threshold = self.config.critical_risk_threshold

        self._history: dict[str, np.ndarray] = {}
        self._global_history: np.ndarray = np.asarray([], dtype=float)
        self._stage_history: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------
    def fit(self, tickets: Iterable[JiraTicket]) -> "SLAForecaster":
        """Learn resolution-time and stage-dwell distributions."""

        tickets = list(tickets)
        per_priority: dict[str, list[float]] = {}
        stage_buckets: dict[str, list[float]] = {}
        all_values: list[float] = []

        for ticket in tickets:
            hours = ticket.resolution_hours(self.hours_per_day)
            if hours is not None and hours > 0:
                per_priority.setdefault(ticket.priority.value, []).append(hours)
                all_values.append(hours)
            for stage, dwell in ticket.stage_dwell_hours(self.hours_per_day).items():
                if dwell > 0:
                    stage_buckets.setdefault(stage, []).append(dwell)

        self._history = {
            key: np.sort(np.asarray(values, dtype=float))
            for key, values in per_priority.items()
        }
        self._global_history = np.sort(np.asarray(all_values, dtype=float))
        self._stage_history = {
            key: np.asarray(values, dtype=float) for key, values in stage_buckets.items()
        }
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _series_for(self, priority: str) -> tuple[np.ndarray, str]:
        series = self._history.get(priority)
        if series is not None and series.size >= self.min_history:
            return series, "empirical-hazard"
        if self._global_history.size >= self.min_history:
            return self._global_history, "global-hazard"
        return np.asarray([], dtype=float), "ratio-fallback"

    @staticmethod
    def _survival(series: np.ndarray, point: float) -> float:
        if series.size == 0:
            return 1.0
        return float(np.mean(series > point))

    def _base_probability(self, series: np.ndarray, elapsed: float, target: float) -> float:
        if series.size == 0:
            ratio = elapsed / target if target > 0 else 1.0
            logistic = 1.0 / (1.0 + np.exp(-3.0 * (ratio - 1.0)))
            return float(min(99.0, max(1.0, logistic * 100.0)))

        survived = self._survival(series, elapsed)
        if survived <= 1e-6:
            # No historical ticket ever survived this long -> extreme risk.
            return 99.0
        conditional = self._survival(series, target) / survived
        return float(min(100.0, max(0.0, conditional * 100.0)))

    def _stage_signal(self, ticket: JiraTicket, reference: datetime | None) -> tuple[float, float]:
        """Return (stage_pressure 0-1, historical median dwell hours)."""

        stage = ticket.status.value
        dwell = ticket.current_stage_dwell_hours(reference, self.hours_per_day)
        history = self._stage_history.get(stage)
        if history is None or history.size == 0:
            return 0.5, 0.0
        median = float(np.median(history))
        if median <= 0:
            return 0.5, 0.0
        pressure = float(min(1.5, dwell / median))
        return pressure, median

    @staticmethod
    def _escalation_text(level: RiskLevel, priority: str, runway: float) -> str:
        if level is RiskLevel.CRITICAL:
            return (
                "Escalate now: page the on-call Tier 2 engineer and notify the "
                "account owner; breach is imminent."
            )
        if level is RiskLevel.HIGH:
            return (
                "Prioritise in the next triage: assign a senior engineer and post "
                "a proactive customer update."
            )
        if level is RiskLevel.MEDIUM:
            return "Monitor closely and refresh the customer with a status update."
        if runway <= 0:
            return "SLA already breached: log a root-cause review and close out."
        return "No action required; comfortably within SLA runway."

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------
    def predict(
        self, ticket: JiraTicket, reference: datetime | None = None
    ) -> BreachRiskPrediction:
        """Score a single active ticket."""

        reference = reference or datetime.now(timezone.utc)
        target = self.policy.target_for(ticket.priority).resolution_hours
        elapsed = max(0.0, ticket.age_hours(reference, self.hours_per_day))
        runway = target - elapsed

        series, method = self._series_for(ticket.priority.value)
        base = self._base_probability(series, elapsed, target)

        pressure, stage_median = self._stage_signal(ticket, reference)
        stage_score = float(min(100.0, pressure * 66.0))

        probability = 0.7 * base + 0.3 * stage_score
        if runway <= 0:
            probability = max(probability, 92.0)
        probability = float(min(100.0, max(0.0, probability)))

        level = RiskLevel.from_probability(
            probability, self.imminent_threshold, self.critical_threshold
        )
        return BreachRiskPrediction(
            key=ticket.key,
            priority=ticket.priority,
            status=ticket.status.value,
            age_hours=round(elapsed, 3),
            sla_target_hours=round(target, 3),
            runway_hours=round(runway, 3),
            breach_probability=round(probability, 2),
            risk_level=level,
            escalation_recommendation=self._escalation_text(level, ticket.priority.value, runway),
            current_stage_dwell_hours=round(
                ticket.current_stage_dwell_hours(reference, self.hours_per_day), 3
            ),
            historical_stage_median_hours=round(stage_median, 3),
            sample_size=int(series.size),
            method=method,
        )

    def predict_all(
        self, tickets: Iterable[JiraTicket], reference: datetime | None = None
    ) -> list[BreachRiskPrediction]:
        """Score every unresolved ticket, most at-risk first."""

        reference = reference or datetime.now(timezone.utc)
        predictions = [
            self.predict(ticket, reference)
            for ticket in tickets
            if not ticket.is_resolved
        ]
        return sorted(predictions, key=lambda p: p.breach_probability, reverse=True)

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------
    def breach_radar(
        self, tickets: Iterable[JiraTicket], reference: datetime | None = None
    ) -> pd.DataFrame:
        """Return the at-risk backlog as a DataFrame for the dashboard."""

        predictions = self.predict_all(tickets, reference)
        if not predictions:
            return pd.DataFrame(
                columns=[
                    "key",
                    "priority",
                    "status",
                    "age_hours",
                    "sla_target_hours",
                    "runway_hours",
                    "breach_probability",
                    "risk_level",
                    "escalation_recommendation",
                ]
            )
        return pd.DataFrame([p.to_row() for p in predictions])

    def summary(self, tickets: Iterable[JiraTicket]) -> dict[str, int]:
        """Count active tickets per risk bucket."""

        counts = {level.value: 0 for level in RiskLevel}
        for prediction in self.predict_all(tickets):
            counts[prediction.risk_level.value] += 1
        counts["total_active"] = sum(
            1 for ticket in tickets if not ticket.is_resolved
        )
        return counts
