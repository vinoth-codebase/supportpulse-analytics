"""Tests for the statistical SLA breach forecaster."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.config import ForecastConfig
from supportpulse.models.metrics import RiskLevel
from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket, Priority, Status, Transition

UTC = timezone.utc
REFERENCE = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)


def make_open_ticket(key: str, priority: Priority, age_hours: float, status=Status.IN_PROGRESS):
    created = REFERENCE - timedelta(hours=age_hours)
    return JiraTicket(
        key=key,
        priority=priority,
        status=status,
        created_at=created,
        assignee="Tier 2 Engineer",
        component="Network Stack",
        transitions=[
            Transition(
                from_status="Open",
                to_status="In Progress",
                at=created + timedelta(hours=1),
                tier="Tier 2",
            )
        ],
    )


class TestForecasterFit:
    def test_fit_builds_history(self, forecaster, tickets):
        forecaster.fit(tickets)
        assert forecaster._global_history.size > 0
        assert forecaster._history

    def test_series_for_uses_priority_history(self, forecaster, tickets):
        forecaster.fit(tickets)
        priority = tickets[0].priority.value
        series, method = forecaster._series_for(priority)
        assert series.size > 0
        assert method in {"empirical-hazard", "global-hazard"}

    def test_series_for_fallback_without_history(self):
        forecaster = SLAForecaster()
        series, method = forecaster._series_for("P1-Critical")
        assert series.size == 0
        assert method == "ratio-fallback"


class TestForecasterPrediction:
    def test_predict_open_ticket(self, forecaster, tickets, open_ticket):
        forecaster.fit(tickets)
        prediction = forecaster.predict(open_ticket, REFERENCE)
        assert 0.0 <= prediction.breach_probability <= 100.0
        assert prediction.key == open_ticket.key
        assert prediction.risk_level in set(RiskLevel)
        assert prediction.escalation_recommendation

    def test_breached_ticket_is_critical(self, forecaster, tickets):
        forecaster.fit(tickets)
        overdue = make_open_ticket("SUP-OVERDUE", Priority.P1, age_hours=200)
        prediction = forecaster.predict(overdue, REFERENCE)
        assert prediction.runway_hours < 0
        assert prediction.is_breached
        assert prediction.breach_probability >= 92.0
        assert prediction.risk_level is RiskLevel.CRITICAL

    def test_safe_ticket_is_low_risk(self, forecaster, tickets):
        forecaster.fit(tickets)
        fresh = make_open_ticket("SUP-FRESH", Priority.P4, age_hours=1)
        prediction = forecaster.predict(fresh, REFERENCE)
        assert prediction.runway_hours > 0
        assert prediction.risk_level in {RiskLevel.LOW, RiskLevel.MEDIUM}

    def test_fallback_probability_without_history(self):
        forecaster = SLAForecaster()
        ticket = make_open_ticket("SUP-NOHIST", Priority.P3, age_hours=40)
        prediction = forecaster.predict(ticket, REFERENCE)
        assert prediction.method == "ratio-fallback"
        assert 0.0 <= prediction.breach_probability <= 100.0

    def test_global_fallback_when_priority_sparse(self, forecaster, tickets):
        # Fit on a single priority only; other priorities should use the global curve.
        forecaster.fit([t for t in tickets if t.priority is Priority.P3])
        series, method = forecaster._series_for("P1-Critical")
        assert method == "global-hazard"
        assert series.size > 0

    def test_predict_all_sorted_and_excludes_resolved(self, forecaster, tickets, sample_ticket):
        forecaster.fit(tickets)
        population = tickets + [sample_ticket]
        predictions = forecaster.predict_all(population, REFERENCE)
        assert all(p.key != sample_ticket.key for p in predictions)
        probabilities = [p.breach_probability for p in predictions]
        assert probabilities == sorted(probabilities, reverse=True)

    def test_breach_radar(self, forecaster, tickets):
        forecaster.fit(tickets)
        radar = forecaster.breach_radar(tickets, REFERENCE)
        assert not radar.empty
        assert "risk_level" in radar.columns

    def test_breach_radar_empty(self, forecaster, sample_ticket):
        forecaster.fit([sample_ticket])
        radar = forecaster.breach_radar([sample_ticket], REFERENCE)
        assert radar.empty
        assert "risk_level" in radar.columns

    def test_summary(self, forecaster, tickets):
        forecaster.fit(tickets)
        summary = forecaster.summary(tickets)
        assert summary["total_active"] == sum(1 for t in tickets if not t.is_resolved)
        assert sum(summary[k] for k in ("Low", "Medium", "High", "Critical")) == summary[
            "total_active"
        ]


class TestForecasterInternals:
    def test_base_probability_fallback_curve(self, forecaster):
        probability = forecaster._base_probability(np.asarray([]), 20.0, 24.0)
        assert 0.0 <= probability <= 100.0

    def test_base_probability_survival_ratio(self, forecaster):
        history = np.asarray([1.0, 2.0, 3.0, 100.0])
        probability = forecaster._base_probability(history, 2.0, 4.0)
        assert 0.0 <= probability <= 100.0

    def test_base_probability_extreme(self, forecaster):
        history = np.asarray([1.0, 2.0])
        assert forecaster._base_probability(history, 50.0, 24.0) == 99.0

    def test_stage_signal_default(self, forecaster, open_ticket):
        pressure, median = forecaster._stage_signal(open_ticket, REFERENCE)
        assert pressure == 0.5
        assert median == 0.0

    def test_stage_signal_with_history(self, forecaster, tickets):
        forecaster.fit(tickets)
        ticket = next(t for t in tickets if not t.is_resolved)
        pressure, median = forecaster._stage_signal(ticket, REFERENCE)
        assert pressure > 0
        assert median >= 0

    @pytest.mark.parametrize(
        "level",
        [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL],
    )
    def test_escalation_text(self, level):
        text = SLAForecaster._escalation_text(level, "P1-Critical", 5.0)
        assert isinstance(text, str) and text

    def test_custom_thresholds(self, policy, tickets):
        config = ForecastConfig(
            imminent_risk_threshold=40.0,
            critical_risk_threshold=50.0,
            minimum_history_samples=2,
            ping_pong_threshold=2,
        )
        forecaster = SLAForecaster(policy, config=config)
        forecaster.fit(tickets)
        ticket = make_open_ticket("SUP-T", Priority.P2, age_hours=20)
        prediction = forecaster.predict(ticket, REFERENCE)
        assert prediction.breach_probability <= 100.0

    def test_handles_ticket_without_transitions(self, forecaster, tickets):
        forecaster.fit(tickets)
        ticket = JiraTicket(
            key="SUP-PLAIN",
            priority=Priority.P3,
            status=Status.OPEN,
            created_at=REFERENCE - timedelta(hours=10),
        )
        prediction = forecaster.predict(ticket, REFERENCE)
        assert prediction.key == "SUP-PLAIN"


class TestForecasterWithEmptyPolicy:
    def test_target_fallback(self):
        forecaster = SLAForecaster(SLAPolicy(name="none", targets={}))
        ticket = make_open_ticket("SUP-F", Priority.P1, age_hours=5)
        prediction = forecaster.predict(ticket, REFERENCE)
        assert prediction.sla_target_hours == 8.0
