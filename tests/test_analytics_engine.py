"""Tests for the KPI engine, cycle-time analytics and metric models."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from supportpulse.analytics.cycle_times import CycleTimeCalculator
from supportpulse.analytics.metrics_engine import MetricsEngine, _rag, _safe_mean
from supportpulse.models.metrics import (
    BreachRiskPrediction,
    RiskLevel,
    SupportMetrics,
)
from supportpulse.models.sla import SLAPolicy, SLATarget
from supportpulse.models.ticket import JiraTicket, Priority, Status

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Metrics engine
# ---------------------------------------------------------------------------
class TestMetricsEngine:
    def test_compute_basic(self, engine, tickets):
        metrics = engine.compute(tickets)
        assert metrics.total_tickets == len(tickets)
        assert metrics.resolved_tickets + metrics.open_tickets == metrics.total_tickets
        assert metrics.mttr_hours > 0
        assert metrics.p50_hours <= metrics.p85_hours <= metrics.p95_hours
        assert 0.0 <= metrics.fcr_rate <= 1.0
        assert 0.0 <= metrics.breach_rate <= 1.0
        assert metrics.window_days >= 1

    def test_compute_empty(self, engine):
        metrics = engine.compute([])
        assert metrics.total_tickets == 0
        assert metrics.mttr_hours == 0.0

    def test_by_priority_and_status(self, engine, tickets):
        metrics = engine.compute(tickets)
        assert metrics.by_priority
        assert metrics.by_component
        assert metrics.by_status
        for values in metrics.by_priority.values():
            assert "mttr_hours" in values
            assert "breach_rate" in values

    def test_window_days_override(self, policy, tickets):
        engine = MetricsEngine(policy, window_days=10)
        metrics = engine.compute(tickets)
        assert metrics.window_days == 10
        assert metrics.throughput_per_day == pytest.approx(
            metrics.resolved_tickets / 10, rel=1e-6
        )

    def test_to_frame(self, engine, tickets):
        frame = engine.to_frame(tickets)
        assert isinstance(frame, pd.DataFrame)
        assert "resolution_hours" in frame.columns
        assert "created_date" in frame.columns

    def test_to_frame_empty(self, engine):
        assert engine.to_frame([]).empty

    def test_sla_compliance_table(self, engine, tickets):
        table = engine.sla_compliance_table(tickets)
        assert not table.empty
        assert set(table["status"]).issubset({"Green", "Amber", "Red"})
        assert table["attainment"].between(0.0, 1.0).all()

    def test_sla_compliance_table_empty(self, engine):
        assert engine.sla_compliance_table([]).empty

    def test_sla_compliance_from_metrics(self, engine, tickets):
        metrics = engine.compute(tickets)
        table = engine.sla_compliance_table_from_metrics(metrics)
        assert not table.empty
        assert list(table.columns) == ["priority", "resolved", "attainment", "status"]

    def test_sla_compliance_from_empty_metrics(self, engine):
        table = engine.sla_compliance_table_from_metrics(SupportMetrics())
        assert table.empty

    def test_component_hotspots(self, engine, tickets):
        hotspots = engine.component_hotspots(tickets, top_n=5)
        assert len(hotspots) <= 5
        assert hotspots["breaches"].is_monotonic_decreasing

    def test_component_hotspots_empty(self, engine):
        assert engine.component_hotspots([]).empty

    def test_mttd(self, engine, tickets):
        assert engine.compute(tickets).mttd_hours >= 0

    def test_helpers(self):
        assert _safe_mean(np.asarray([], dtype=float)) == 0.0
        assert _safe_mean(np.asarray([2.0, 4.0])) == 3.0
        assert _rag(0.95) == "Green"
        assert _rag(0.8) == "Amber"
        assert _rag(0.5) == "Red"


# ---------------------------------------------------------------------------
# Cycle times
# ---------------------------------------------------------------------------
class TestCycleTimeCalculator:
    def test_resolution_series(self, cycle_times, tickets):
        series = cycle_times.resolution_series(tickets)
        assert series.size > 0
        assert np.all(np.diff(series) >= 0)

    def test_first_response_series(self, cycle_times, tickets):
        assert cycle_times.first_response_series(tickets).size > 0

    def test_percentile_and_empty(self, cycle_times):
        values = np.asarray([1.0, 2.0, 3.0, 4.0])
        assert cycle_times.percentile(values, 50) == pytest.approx(2.5)
        assert cycle_times.percentile([], 90) == 0.0

    def test_percentiles_mapping(self, cycle_times):
        result = cycle_times.percentiles([1.0, 2.0, 3.0], (50, 85, 95))
        assert set(result) == {"p50", "p85", "p95"}

    def test_ecdf(self, cycle_times):
        x, y = cycle_times.ecdf([3.0, 1.0, 2.0])
        assert list(x) == [1.0, 2.0, 3.0]
        assert y[-1] == 1.0

    def test_ecdf_empty(self, cycle_times):
        x, y = cycle_times.ecdf([])
        assert list(x) == [0.0]

    def test_ecdf_at_and_survival(self, cycle_times):
        values = [1.0, 2.0, 3.0, 4.0]
        assert cycle_times.ecdf_at(values, 2.0) == 0.5
        assert cycle_times.survival_at(values, 2.0) == 0.5
        assert cycle_times.ecdf_at([], 1.0) == 0.0
        assert cycle_times.survival_at([], 1.0) == 1.0

    def test_recommend_slos(self, cycle_times, tickets, policy):
        recommendations = cycle_times.recommend_slos(tickets, policy)
        assert recommendations
        for entry in recommendations.values():
            assert "empirical_slo_hours" in entry
            assert "attainable" in entry

    def test_cumulative_flow(self, cycle_times, tickets):
        flow = cycle_times.cumulative_flow(tickets)
        assert not flow.empty
        assert "WIP" in flow.columns
        assert "Done" in flow.columns
        assert flow["WIP"].iloc[-1] >= 0

    def test_cumulative_flow_empty(self, cycle_times):
        assert cycle_times.cumulative_flow([]).empty

    def test_status_at(self, cycle_times, sample_ticket):
        moment = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)
        assert cycle_times._status_at(sample_ticket, moment) == "In Triage"
        before = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
        assert cycle_times._status_at(sample_ticket, before) == "Open"

    def test_aging_wip(self, cycle_times, tickets):
        aging = cycle_times.aging_wip(tickets)
        assert not aging.empty
        assert aging["age_hours"].is_monotonic_decreasing

    def test_aging_wip_empty(self, cycle_times, sample_ticket):
        assert cycle_times.aging_wip([sample_ticket]).empty

    def test_inflow_outflow(self, cycle_times, tickets, reference):
        flow = cycle_times.inflow_outflow(tickets, window_days=30, reference=reference)
        assert "cumulative_backlog" in flow.columns
        assert flow["inflow"].sum() > 0


# ---------------------------------------------------------------------------
# SLA policy / target models
# ---------------------------------------------------------------------------
class TestSLAPolicy:
    def test_default_policy(self):
        policy = SLAPolicy.default()
        assert policy.target_for(Priority.P1).resolution_hours == 8.0
        assert policy.resolution_targets["P2-High"] == 24.0

    def test_target_for_unknown_priority_fallback(self):
        policy = SLAPolicy(name="Empty", targets={})
        target = policy.target_for(Priority.P1)
        assert target.resolution_hours == 8.0
        assert policy.target_for("P4-Low").resolution_hours == 120.0

    def test_target_for_by_enum_lookup(self):
        policy = SLAPolicy.default()
        assert policy.target_for("P3-Medium").priority is Priority.P3

    def test_from_yaml(self, policy_yaml):
        policy = SLAPolicy.from_yaml(policy_yaml)
        assert policy.name == "Test Policy"
        assert policy.target_for(Priority.P1).resolution_hours == 8.0
        assert policy.target_for(Priority.P3).resolution_hours == 56.0

    def test_escalation_default(self):
        target = SLATarget(priority=Priority.P2, response_hours=4, resolution_hours=24)
        assert target.escalation_hours == pytest.approx(15.84, rel=1e-2)

    def test_explicit_escalation(self):
        target = SLATarget(
            priority=Priority.P2, response_hours=4, resolution_hours=24, escalation_hours=10
        )
        assert target.escalation_hours == 10


# ---------------------------------------------------------------------------
# Metric models
# ---------------------------------------------------------------------------
class TestMetricModels:
    def test_support_metrics_summary_rows(self):
        metrics = SupportMetrics(total_tickets=5, resolved_tickets=3, open_tickets=2)
        rows = metrics.as_summary_rows()
        assert any(label == "Total tickets" for label, _ in rows)

    def test_risk_level_thresholds(self):
        assert RiskLevel.from_probability(95) is RiskLevel.CRITICAL
        assert RiskLevel.from_probability(70) is RiskLevel.HIGH
        assert RiskLevel.from_probability(40) is RiskLevel.MEDIUM
        assert RiskLevel.from_probability(10) is RiskLevel.LOW

    def test_breach_prediction_row_and_flags(self):
        prediction = BreachRiskPrediction(
            key="SUP-1",
            priority=Priority.P2,
            status="In Progress",
            age_hours=30,
            sla_target_hours=24,
            runway_hours=-6,
            breach_probability=97.5,
            risk_level=RiskLevel.CRITICAL,
        )
        assert prediction.is_breached
        row = prediction.to_row()
        assert row["key"] == "SUP-1"
        assert row["risk_level"] == "Critical"

    def test_breach_prediction_probability_bounds(self):
        with pytest.raises(Exception):
            BreachRiskPrediction(
                key="SUP-1",
                priority=Priority.P2,
                status="Open",
                age_hours=1,
                sla_target_hours=24,
                runway_hours=23,
                breach_probability=150,
            )

    def test_breach_prediction_priority_coercion(self):
        prediction = BreachRiskPrediction(
            key="SUP-2",
            priority="P1",
            status="Open",
            age_hours=1,
            sla_target_hours=8,
            runway_hours=7,
            breach_probability=10,
        )
        assert prediction.priority is Priority.P1
