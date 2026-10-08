"""Tests for the extraction layer, JQL builder, changelog parser and config."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from supportpulse import config as config_module
from supportpulse.config import AppConfig, JiraConfig, load_config, resolve_output_dir
from supportpulse.extractor.changelog_parser import ChangelogParser, _parse_datetime
from supportpulse.extractor.jira_client import (
    JiraClient,
    generate_mock_issues,
    parse_issue,
)
from supportpulse.extractor.jql_builder import JQLBuilder
from supportpulse.models.ticket import (
    JiraTicket,
    Priority,
    Status,
    Transition,
    business_hours_between,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fakes for HTTP
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _issue_payload(key: str, status: str = "Open", created: str | None = None) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"summary for {key}",
            "priority": {"name": "P2-High"},
            "status": {"name": status},
            "created": created or "2026-10-01T09:00:00+00:00",
            "resolutiondate": None,
            "assignee": {"displayName": "Priya Menon"},
            "components": [{"name": "Installer"}],
            "labels": ["regression"],
        },
        "changelog": {
            "histories": [
                {
                    "created": "2026-10-01T10:00:00+00:00",
                    "author": {"displayName": "Priya Menon"},
                    "items": [
                        {
                            "field": "status",
                            "fromString": "Open",
                            "toString": "In Triage",
                        },
                        {"field": "assignee", "fromString": "", "toString": "Tier 1"},
                    ],
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# JQL builder
# ---------------------------------------------------------------------------
class TestJQLBuilder:
    def test_support_project_default_query(self):
        jql = JQLBuilder.for_support_project("SUP", 30).build()
        assert jql == "project = SUP AND updated >= -30d ORDER BY created DESC"

    def test_open_only_query(self):
        jql = JQLBuilder.for_support_project("SUP", 14, open_only=True).build()
        assert 'status not in ("Resolved", "Closed")' in jql
        assert jql.endswith("ORDER BY created DESC")

    def test_chainable_filters(self):
        jql = (
            JQLBuilder("SUP")
            .where_project()
            .priority_in([Priority.P1, "P2-High"])
            .component("Network Stack")
            .assignee("Unassigned")
            .labels_in(["escalated"])
            .text_search('signal "loss"')
            .raw("created >= -7d")
            .order_by("updated", "ASC")
            .build()
        )
        assert 'priority in ("P1-Critical", "P2-High")' in jql
        assert 'component = "Network Stack"' in jql
        assert "assignee is EMPTY" in jql
        assert 'labels in ("escalated")' in jql
        assert 'text ~ "signal \\"loss\\""' in jql
        assert jql.endswith("ORDER BY updated ASC")

    def test_status_in_and_not_in(self):
        jql = (
            JQLBuilder("SUP")
            .status_in([Status.OPEN, Status.IN_PROGRESS])
            .status_not_in(["Closed"])
            .build()
        )
        assert 'status in ("Open", "In Progress")' in jql
        assert 'status not in ("Closed")' in jql

    def test_created_since_and_assignee_named(self):
        jql = JQLBuilder("SUP").created_since(5).assignee("Lukas Berger").build()
        assert "created >= -5d" in jql
        assert 'assignee = "Lukas Berger"' in jql

    def test_invalid_days(self):
        with pytest.raises(ValueError):
            JQLBuilder("SUP").updated_since(0)

    def test_invalid_direction(self):
        with pytest.raises(ValueError):
            JQLBuilder("SUP").order_by("created", "SIDEWAYS")

    def test_repr_and_str(self):
        builder = JQLBuilder("SUP").where_project()
        assert "project = SUP" in str(builder)
        assert "JQLBuilder" in repr(builder)


# ---------------------------------------------------------------------------
# Changelog parser
# ---------------------------------------------------------------------------
class TestChangelogParser:
    def test_parse_histories(self, parser):
        raw = _issue_payload("SUP-1")["changelog"]["histories"]
        transitions = parser.parse_histories(raw)
        assert len(transitions) == 1
        assert transitions[0].from_status == "Open"
        assert transitions[0].to_status == "In Triage"
        assert transitions[0].tier == "Tier 1"

    def test_parse_issue(self, parser):
        transitions = parser.parse_issue(_issue_payload("SUP-2"))
        assert transitions and transitions[0].to_enum is Status.IN_TRIAGE

    def test_parse_histories_skips_bad_dates(self, parser):
        raw = [{"created": None, "items": []}]
        assert parser.parse_histories(raw) == []

    def test_tier_only_reassignment(self, parser):
        raw = [
            {
                "created": "2026-10-01T10:00:00+00:00",
                "author": {"displayName": "Automation"},
                "items": [
                    {"field": "assignee", "fromString": "", "toString": "Tier 2"}
                ],
            }
        ]
        transitions = parser.parse_histories(raw)
        assert transitions[0].tier == "Tier 2"
        assert transitions[0].to_status == ""

    def test_dwell_analysis(self, parser, sample_ticket):
        analysis = parser.dwell_analysis(sample_ticket)
        assert analysis["key"] == sample_ticket.key
        assert analysis["total_hours"] > 0
        assert analysis["dominant_stage"] is not None

    def test_lifecycle_path(self, parser, sample_ticket):
        path = parser.lifecycle_path(sample_ticket)
        assert path[0] == "Open"
        assert "Resolved" in path

    def test_stage_bottlenecks_and_matrix(self, parser, tickets):
        bottlenecks = parser.stage_bottlenecks(tickets)
        assert bottlenecks
        matrix = parser.transition_matrix(tickets)
        assert isinstance(matrix, dict)

    def test_ping_pong_detection(self, parser):
        created = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        transitions = []
        tiers = ["Tier 1", "Tier 2", "Development", "Tier 1"]
        for index, tier in enumerate(tiers):
            transitions.append(
                Transition(
                    from_status="In Progress",
                    to_status="In Progress",
                    at=created + timedelta(hours=index + 1),
                    tier=tier,
                )
            )
        ticket = JiraTicket(
            key="SUP-PP",
            created_at=created,
            transitions=transitions,
            status=Status.IN_PROGRESS,
        )
        assert ticket.is_ping_pong(3) is True
        assert parser.ping_pong_tickets([ticket]) == [ticket]

    def test_parse_datetime_variants(self):
        assert _parse_datetime(None) is None
        assert _parse_datetime("") is None
        assert _parse_datetime("2026-10-01T09:00:00Z").year == 2026
        assert _parse_datetime("2026-10-01T09:00:00+0200").utcoffset().total_seconds() == 7200
        assert _parse_datetime("not-a-date") is None
        already = datetime(2026, 1, 1, tzinfo=UTC)
        assert _parse_datetime(already) is already


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class TestTicketModel:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("P1", Priority.P1),
            ("critical", Priority.P1),
            ("Highest", Priority.P1),
            ("P2-High", Priority.P2),
            ("major", Priority.P2),
            ("P3", Priority.P3),
            ("Minor", Priority.P3),
            ("P4-Low", Priority.P4),
            ("unknown", Priority.P3),
        ],
    )
    def test_priority_coercion(self, raw, expected):
        assert Priority.from_any(raw) is expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Open", Status.OPEN),
            ("in progress", Status.IN_PROGRESS),
            ("Waiting for Vendor", Status.WAITING_VENDOR),
            ("done", Status.RESOLVED),
            ("closed", Status.CLOSED),
            ("mystery", Status.OPEN),
        ],
    )
    def test_status_coercion(self, raw, expected):
        assert Status.from_any(raw) is expected

    def test_terminal_flag(self):
        assert Status.RESOLVED.is_terminal
        assert not Status.OPEN.is_terminal

    def test_business_hours_between(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # Thursday
        end = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        assert business_hours_between(start, end) == 3.0

    def test_business_hours_skips_weekend(self):
        friday = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
        monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
        assert business_hours_between(friday, monday) == 2.0

    def test_business_hours_edge_cases(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        assert business_hours_between(None, start) == 0.0
        assert business_hours_between(start, None) == 0.0
        assert business_hours_between(start, start) == 0.0

    def test_resolution_and_age(self, sample_ticket):
        assert sample_ticket.is_resolved
        assert sample_ticket.resolution_hours() > 0
        assert sample_ticket.age_hours() == sample_ticket.resolution_hours()
        assert sample_ticket.first_response_hours() == 1.0

    def test_stage_dwell_and_current(self, sample_ticket, open_ticket):
        dwell = sample_ticket.stage_dwell_hours()
        assert dwell["Open"] == 1.0
        assert dwell["In Triage"] == 4.0
        # A resolved ticket has zero dwell in its terminal stage…
        assert sample_ticket.current_stage_dwell_hours() == 0.0
        # …whereas an in-flight ticket accumulates dwell in its current stage.
        assert open_ticket.current_stage_dwell_hours() > 0

    def test_no_transitions_dwell(self):
        ticket = JiraTicket(
            key="SUP-X",
            created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            status=Status.OPEN,
        )
        dwell = ticket.stage_dwell_hours()
        assert "Open" in dwell
        assert ticket.first_response_hours() is None
        assert ticket.reassignment_count() == 0

    def test_effective_resolved_from_transitions(self):
        created = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        resolved_at = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        ticket = JiraTicket(
            key="SUP-Y",
            created_at=created,
            status=Status.RESOLVED,
            transitions=[
                Transition(from_status="In Progress", to_status="Resolved", at=resolved_at)
            ],
        )
        assert ticket.effective_resolved_at == resolved_at
        assert ticket.resolution_hours() == 6.0

    def test_ledger_row(self, sample_ticket):
        row = sample_ticket.to_ledger_row()
        assert row["key"] == sample_ticket.key
        assert row["is_resolved"] is True
        assert row["component"] == "Installer"


# ---------------------------------------------------------------------------
# Jira client
# ---------------------------------------------------------------------------
class TestJiraClientMock:
    def test_mock_search_pagination(self, client):
        issues = client.search("project = SUP")
        assert len(issues) == 60
        assert client.page_count >= 1

    def test_extract_tickets(self, tickets):
        assert len(tickets) == 60
        assert all(t.key.startswith("SUP-") for t in tickets)

    def test_progress_callback(self, client):
        seen = []

        def callback(page, fetched, total):
            seen.append((page, fetched, total))

        client.search("project = SUP", max_results=10, progress=callback)
        assert seen and seen[0][2] == 60

    def test_fetch_changelog_mock(self, client):
        histories = client.fetch_changelog("SUP-1001")
        assert isinstance(histories, list)
        assert client.fetch_changelog("NOPE-1") == []

    def test_load_dump(self, tmp_path, mock_issues):
        dump = tmp_path / "dump.json"
        dump.write_text(json.dumps({"issues": mock_issues[:5]}), encoding="utf-8")
        client = JiraClient(mock_mode=False)
        loaded = client.load_dump(dump)
        assert len(loaded) == 5
        assert client.mock_mode is True

    def test_load_dump_list_form(self, tmp_path, mock_issues):
        dump = tmp_path / "dump.json"
        dump.write_text(json.dumps(mock_issues[:3]), encoding="utf-8")
        client = JiraClient(mock_mode=False)
        assert len(client.load_dump(dump)) == 3

    def test_ping_mock(self, client):
        assert client.ping() is True

    def test_skips_malformed_issue(self, client):
        client._mock_dataset = [{"key": "BAD-1", "fields": {"created": None}}]
        client._mock_dataset[0]["fields"]["created"] = "not-a-date"
        tickets = client.extract_tickets("project = SUP")
        assert len(tickets) == 1  # parse falls back to now()


class TestJiraClientLive:
    def _config(self, **overrides) -> JiraConfig:
        base = {
            "base_url": "https://example.atlassian.net",
            "api_version": 3,
            "email": "a@b.com",
            "api_token": "token",
            "page_size": 2,
            "max_retries": 3,
            "backoff_factor": 0.0,
            "rate_limit_pause": 0.0,
        }
        base.update(overrides)
        return JiraConfig(**base)

    def test_live_pagination(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [
                FakeResponse(payload={"total": 3, "issues": [_issue_payload("SUP-1"), _issue_payload("SUP-2")]}),
                FakeResponse(payload={"total": 3, "issues": [_issue_payload("SUP-3")]}),
            ]
        )
        client = JiraClient(self._config(), session=session, mock_mode=False)
        issues = client.search("project = SUP")
        assert len(issues) == 3
        assert client.page_count == 2

    def test_live_search_max_results(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [FakeResponse(payload={"total": 10, "issues": [_issue_payload("SUP-1"), _issue_payload("SUP-2")]})]
        )
        client = JiraClient(self._config(), session=session, mock_mode=False)
        issues = client.search("project = SUP", max_results=1)
        assert len(issues) == 1

    def test_rate_limit_then_success(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [
                FakeResponse(status_code=429, headers={"Retry-After": "0"}),
                FakeResponse(payload={"total": 1, "issues": [_issue_payload("SUP-1")]}),
            ]
        )
        client = JiraClient(self._config(page_size=5), session=session, mock_mode=False)
        issues = client.search("project = SUP")
        assert len(issues) == 1

    def test_transient_then_success(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [
                FakeResponse(status_code=503),
                FakeResponse(payload={"total": 1, "issues": [_issue_payload("SUP-1")]}),
            ]
        )
        client = JiraClient(self._config(page_size=5), session=session, mock_mode=False)
        assert len(client.search("project = SUP")) == 1

    def test_client_error_raises(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession([FakeResponse(status_code=400, text="bad request")])
        client = JiraClient(self._config(), session=session, mock_mode=False)
        with pytest.raises(RuntimeError):
            client.search("project = SUP")

    def test_exhausted_retries_raises(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession([FakeResponse(status_code=503) for _ in range(3)])
        client = JiraClient(self._config(), session=session, mock_mode=False)
        with pytest.raises(RuntimeError):
            client.search("project = SUP")

    def test_network_exception_retried(self, monkeypatch):
        import requests

        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [
                requests.RequestException("boom"),
                FakeResponse(payload={"total": 1, "issues": [_issue_payload("SUP-1")]}),
            ]
        )
        client = JiraClient(self._config(page_size=5), session=session, mock_mode=False)
        assert len(client.search("project = SUP")) == 1

    def test_fetch_changelog_live(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession(
            [FakeResponse(payload={"values": [{"created": "2026-10-01T10:00:00+00:00", "items": []}]})]
        )
        client = JiraClient(self._config(), session=session, mock_mode=False)
        assert len(client.fetch_changelog("SUP-1")) == 1

    def test_ping_live(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession([FakeResponse(payload={"name": "tester"})])
        client = JiraClient(self._config(), session=session, mock_mode=False)
        assert client.ping() is True

    def test_ping_live_failure(self, monkeypatch):
        monkeypatch.setattr("supportpulse.extractor.jira_client.time.sleep", lambda *_: None)
        session = FakeSession([FakeResponse(status_code=400, text="nope")])
        client = JiraClient(self._config(), session=session, mock_mode=False)
        assert client.ping() is False

    def test_bearer_token_header(self):
        config = JiraConfig(api_token="abc", email=None)
        client = JiraClient(config, mock_mode=False)
        assert client._headers()["Authorization"] == "Bearer abc"
        assert client._auth() is None


# ---------------------------------------------------------------------------
# parse_issue / mock generator
# ---------------------------------------------------------------------------
class TestParseIssue:
    def test_parse_flat_fields(self):
        ticket = parse_issue(_issue_payload("SUP-77"))
        assert ticket.key == "SUP-77"
        assert ticket.priority is Priority.P2
        assert ticket.component == "Installer"
        assert ticket.labels == ["regression"]

    def test_parse_handles_missing_optional_fields(self):
        raw = {"key": "SUP-78", "fields": {"created": "2026-10-01T09:00:00+00:00"}}
        ticket = parse_issue(raw)
        assert ticket.assignee is None
        assert ticket.component is None

    def test_generate_mock_issues_deterministic(self):
        first = generate_mock_issues(count=10, seed=99)
        second = generate_mock_issues(count=10, seed=99)
        assert [i["key"] for i in first] == [i["key"] for i in second]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class TestConfig:
    def test_load_default_config(self):
        cfg = load_config()
        assert isinstance(cfg, AppConfig)
        assert cfg.project.key == "SUP"
        assert cfg.jira.api_version == 3

    def test_load_config_from_path(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "app:\n  mock_mode: false\nproject:\n  key: TEST\n  default_days: 7\n",
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.project.key == "TEST"
        assert cfg.app.mock_mode is False

    def test_missing_file_uses_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "does-not-exist.yaml")
        assert cfg.project.key == "SUP"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SUPPORTPULSE_APP__MOCK_MODE", "false")
        monkeypatch.setenv("SUPPORTPULSE_JIRA__PAGE_SIZE", "25")
        cfg = load_config()
        assert cfg.app.mock_mode is False
        assert cfg.jira.page_size == 25

    def test_invalid_yaml_mapping(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(path)

    def test_project_root_and_config_dir(self):
        root = config_module.project_root()
        assert (root / "pyproject.toml").exists()
        assert config_module.config_dir().name == "config"

    def test_project_root_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPPORTPULSE_ROOT", str(tmp_path))
        assert config_module.project_root() == tmp_path

    def test_config_dir_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPPORTPULSE_CONFIG_DIR", str(tmp_path))
        assert config_module.config_dir() == tmp_path

    def test_resolve_output_dir_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPPORTPULSE_OUTPUT_DIR", str(tmp_path / "reports"))
        resolved = resolve_output_dir()
        assert resolved.exists()
        assert resolved.name == "reports"

    def test_get_config_cached(self):
        config_module.get_config.cache_clear()
        assert config_module.get_config() is config_module.get_config()

    def test_search_path_property(self):
        assert JiraConfig(api_version=2).search_path == "/rest/api/2/search"
