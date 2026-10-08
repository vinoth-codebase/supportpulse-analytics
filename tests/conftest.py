"""Shared pytest fixtures for the SupportPulse-Analytics test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from supportpulse.analytics.cycle_times import CycleTimeCalculator
from supportpulse.analytics.metrics_engine import MetricsEngine
from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.extractor.changelog_parser import ChangelogParser
from supportpulse.extractor.jira_client import JiraClient, generate_mock_issues, parse_issue
from supportpulse.models.sla import SLAPolicy, SLATarget
from supportpulse.models.ticket import JiraTicket, Priority, Status, Transition

REFERENCE = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def reference() -> datetime:
    return REFERENCE


@pytest.fixture(scope="session")
def mock_issues() -> list[dict]:
    return generate_mock_issues(count=60, days=45, seed=7, reference=REFERENCE)


@pytest.fixture()
def client(mock_issues) -> JiraClient:
    return JiraClient(mock_mode=True, mock_dataset=list(mock_issues))


@pytest.fixture()
def tickets(client) -> list[JiraTicket]:
    return client.extract_tickets("project = SUP")


@pytest.fixture()
def policy() -> SLAPolicy:
    return SLAPolicy.default()


@pytest.fixture()
def parser() -> ChangelogParser:
    return ChangelogParser()


@pytest.fixture()
def engine(policy) -> MetricsEngine:
    return MetricsEngine(policy)


@pytest.fixture()
def cycle_times() -> CycleTimeCalculator:
    return CycleTimeCalculator()


@pytest.fixture()
def forecaster(policy) -> SLAForecaster:
    return SLAForecaster(policy)


@pytest.fixture()
def sample_ticket() -> JiraTicket:
    """A hand-built ticket with a full, well-defined lifecycle."""

    created = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    return JiraTicket(
        key="SUP-9001",
        summary="[P2] Installer fails on Windows 11",
        priority=Priority.P2,
        status=Status.RESOLVED,
        created_at=created,
        resolved_at=datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc),
        assignee="Lukas Berger",
        component="Installer",
        labels=["regression"],
        transitions=[
            Transition(
                from_status="Open",
                to_status="In Triage",
                at=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
                author="Priya Menon",
                tier="Tier 1",
            ),
            Transition(
                from_status="In Triage",
                to_status="In Progress",
                at=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
                author="Lukas Berger",
                tier="Tier 2",
            ),
            Transition(
                from_status="In Progress",
                to_status="Resolved",
                at=datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc),
                author="Lukas Berger",
                tier="Tier 2",
            ),
        ],
    )


@pytest.fixture()
def open_ticket() -> JiraTicket:
    created = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    return JiraTicket(
        key="SUP-9002",
        summary="[P1] Signal loss during acquisition",
        priority=Priority.P1,
        status=Status.IN_PROGRESS,
        created_at=created,
        assignee="Nikhil Sharma",
        component="Network Stack",
        transitions=[
            Transition(
                from_status="Open",
                to_status="In Triage",
                at=datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc),
                tier="Tier 1",
            ),
            Transition(
                from_status="In Triage",
                to_status="In Progress",
                at=datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc),
                tier="Tier 2",
            ),
        ],
    )


@pytest.fixture()
def policy_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "sla_policies.yaml"
    path.write_text(
        "name: Test Policy\n"
        "business_hours_per_day: 8.0\n"
        "business_days: [0, 1, 2, 3, 4]\n"
        "targets:\n"
        "  P1-Critical:\n"
        "    response_hours: 1.0\n"
        "    resolution_hours: 8.0\n"
        "  P3-Medium:\n"
        "    response_hours: 8.0\n"
        "    resolution_hours: 56.0\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "REFERENCE",
    "parse_issue",
    "SLATarget",
]
