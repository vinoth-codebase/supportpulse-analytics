"""Tests for the Rich/Click command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from supportpulse import __version__
from supportpulse.cli import (
    _load_from_dump,
    _resolve_tickets,
    _ticket_to_raw,
    load_policy,
    main,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def dump_path(tmp_path: Path, mock_issues) -> Path:
    path = tmp_path / "raw_jira_dump.json"
    path.write_text(
        json.dumps({"project": "SUP", "total": len(mock_issues), "issues": mock_issues}),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _isolate_output(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTPULSE_OUTPUT_DIR", str(tmp_path / "reports"))


class TestCliBasics:
    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "extract" in result.output
        assert "analyze" in result.output
        assert "report" in result.output

    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output


class TestExtractCommand:
    def test_extract_mock(self, runner, tmp_path: Path):
        output = tmp_path / "dump.json"
        result = runner.invoke(main, ["extract", "--project", "SUP", "--days", "14", "--output", str(output)])
        assert result.exit_code == 0
        assert output.exists()
        payload = json.loads(output.read_text())
        assert payload["total"] > 0
        assert payload["issues"]

    def test_extract_default_output(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPPORTPULSE_OUTPUT_DIR", str(tmp_path / "out"))
        result = runner.invoke(main, ["extract", "--mock"])
        assert result.exit_code == 0
        assert (tmp_path / "out" / "raw_jira_dump.json").exists()


class TestAnalyzeCommand:
    def test_analyze(self, runner, dump_path: Path):
        result = runner.invoke(main, ["analyze", "--input", str(dump_path), "--top", "5"])
        assert result.exit_code == 0
        assert "Support KPIs" in result.output
        assert "SLA Attainment" in result.output
        assert "SLA Breach Risk Radar" in result.output

    def test_analyze_requires_input(self, runner):
        result = runner.invoke(main, ["analyze"])
        assert result.exit_code != 0


class TestReportCommand:
    def test_report_all(self, runner, dump_path: Path, tmp_path: Path):
        monkeypatch_out = tmp_path / "reports"
        result = runner.invoke(main, ["report", "--all", "--input", str(dump_path)])
        assert result.exit_code == 0
        assert (monkeypatch_out / "weekly_support_status.xlsx").exists()
        assert (monkeypatch_out / "executive_status_deck.pptx").exists()
        assert (monkeypatch_out / "weekly_support_status.md").exists()
        assert (monkeypatch_out / "weekly_support_status_confluence.xhtml").exists()

    def test_report_excel_only(self, runner, dump_path: Path, tmp_path: Path):
        result = runner.invoke(main, ["report", "--excel", "--input", str(dump_path)])
        assert result.exit_code == 0
        assert (tmp_path / "reports" / "weekly_support_status.xlsx").exists()
        assert not (tmp_path / "reports" / "executive_status_deck.pptx").exists()

    def test_report_confluence_only(self, runner, dump_path: Path, tmp_path: Path):
        result = runner.invoke(main, ["report", "--confluence", "--input", str(dump_path)])
        assert result.exit_code == 0
        assert (tmp_path / "reports" / "weekly_support_status.md").exists()

    def test_report_pptx_only(self, runner, dump_path: Path, tmp_path: Path):
        result = runner.invoke(main, ["report", "--pptx", "--input", str(dump_path)])
        assert result.exit_code == 0
        assert (tmp_path / "reports" / "executive_status_deck.pptx").exists()

    def test_report_without_input_uses_mock(self, runner, tmp_path: Path):
        result = runner.invoke(main, ["report", "--all"])
        assert result.exit_code == 0
        assert (tmp_path / "reports" / "weekly_support_status.xlsx").exists()


class TestCliHelpers:
    def test_load_from_dump(self, dump_path: Path, mock_issues):
        tickets = _load_from_dump(dump_path)
        assert len(tickets) == len(mock_issues)

    def test_resolve_tickets_from_dump(self, dump_path: Path):
        tickets = _resolve_tickets(str(dump_path), "SUP", 14, None)
        assert tickets

    def test_resolve_tickets_mock(self):
        tickets = _resolve_tickets(None, "SUP", 30, True)
        assert tickets

    def test_ticket_to_raw_roundtrip(self, sample_ticket):
        raw = _ticket_to_raw(sample_ticket)
        assert raw["key"] == sample_ticket.key
        assert raw["fields"]["priority"]["name"] == sample_ticket.priority.value
        assert raw["changelog"]["histories"]

    def test_load_policy(self):
        policy = load_policy()
        assert policy.target_for("P1-Critical").resolution_hours > 0
