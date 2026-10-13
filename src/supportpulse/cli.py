"""SupportPulse command-line interface (Click + Rich)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from supportpulse import __version__
from supportpulse.analytics.metrics_engine import MetricsEngine
from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.config import get_config, project_root, resolve_output_dir
from supportpulse.extractor.jira_client import JiraClient
from supportpulse.extractor.jql_builder import JQLBuilder
from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket
from supportpulse.reporters.confluence_exporter import ConfluenceExporter
from supportpulse.reporters.excel_reporter import ExcelReporter
from supportpulse.reporters.pptx_reporter import PptxReporter

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_policy() -> SLAPolicy:
    """Load the SLA policy, falling back to the built-in default."""

    path = project_root() / "config" / "sla_policies.yaml"
    if path.exists():
        try:
            return SLAPolicy.from_yaml(path)
        except Exception:  # pragma: no cover - defensive
            console.print(f"[yellow]Falling back to default SLA policy ({path}).[/yellow]")
    return SLAPolicy.default()


def _load_from_dump(path: str | Path) -> list[JiraTicket]:
    """Parse tickets from a raw Jira dump JSON file."""

    client = JiraClient(mock_mode=True)
    client.load_dump(path)
    return client.extract_tickets("project = SUP")


def _resolve_tickets(
    input_path: str | None,
    project: str,
    days: int,
    mock: bool | None,
) -> list[JiraTicket]:
    """Resolve the ticket population from a dump file or the Jira client."""

    if input_path:
        return _load_from_dump(input_path)

    client = JiraClient(mock_mode=mock)
    jql = JQLBuilder.for_support_project(project, days).build()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} tickets"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting from Jira", total=None)

        def callback(page: int, fetched: int, total: int | None) -> None:
            if total:
                progress.update(task, total=total, completed=fetched)
            progress.update(task, description=f"Extracting (page {page})")

        tickets = client.extract_tickets(jql, progress=callback)
        progress.update(task, total=len(tickets), completed=len(tickets))

    console.print(
        f"[green]✔[/green] Extracted [bold]{len(tickets)}[/bold] tickets "
        f"via {client.page_count} page(s) "
        f"({'mock' if client.mock_mode else 'live'} mode)."
    )
    return tickets


def _metrics_table(metrics) -> Table:
    table = Table(title="Support KPIs", header_style="bold white on #1F3864")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right", style="bold")
    for label, value in metrics.as_summary_rows():
        table.add_row(label, value)
    return table


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="supportpulse")
def main() -> None:
    """SupportPulse-Analytics — Jira support pipeline & SLA breach forecaster."""


@main.command()
@click.option("--project", default=None, help="Jira project key (default from config).")
@click.option("--days", default=None, type=int, help="Look-back window in days.")
@click.option("--mock/--live", "mock", default=None, help="Force mock or live mode.")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to write the raw dump JSON.",
)
def extract(
    project: str | None, days: int | None, mock: bool | None, output: Path | None
) -> None:
    """Run automated Jira data extraction routines."""

    config = get_config()
    project = project or config.project.key
    days = days or config.project.default_days

    console.rule(f"[bold]SupportPulse extract[/bold] · project={project} · last {days}d")
    tickets = _resolve_tickets(None, project, days, mock)

    if output is None:
        output = resolve_output_dir() / "raw_jira_dump.json"

    payload = {
        "project": project,
        "days": days,
        "extracted_at": tickets[0].created_at.isoformat() if tickets else None,
        "total": len(tickets),
        "issues": [_ticket_to_raw(ticket) for ticket in tickets],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    console.print(f"[green]✔[/green] Wrote raw dump → [bold]{output}[/bold]")


@main.command()
@click.option(
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a raw Jira dump JSON.",
)
@click.option("--top", default=10, type=int, help="Number of at-risk tickets to show.")
def analyze(input_path: Path, top: int) -> None:
    """Compute support metrics and SLA breach risks."""

    console.rule(f"[bold]SupportPulse analyze[/bold] · {input_path.name}")
    tickets = _load_from_dump(input_path)
    policy = load_policy()
    metrics = MetricsEngine(policy).compute(tickets)
    forecaster = SLAForecaster(policy).fit(tickets)

    console.print(_metrics_table(metrics))

    compliance = MetricsEngine(policy).sla_compliance_table(tickets)
    if not compliance.empty:
        table = Table(title="SLA Attainment", header_style="bold white on #2E75B6")
        for column in ["priority", "tickets", "resolved", "sla_target_hours", "attainment", "status"]:
            table.add_column(column.replace("_", " ").title(), justify="right")
        for _, row in compliance.iterrows():
            colour = {"Green": "green", "Amber": "yellow", "Red": "red"}.get(
                str(row["status"]), "white"
            )
            table.add_row(
                str(row["priority"]),
                str(row["tickets"]),
                str(row["resolved"]),
                f"{row['sla_target_hours']:.1f}",
                f"{row['attainment'] * 100:.1f}%",
                f"[{colour}]{row['status']}[/{colour}]",
            )
        console.print(table)

    radar = forecaster.breach_radar(tickets)
    if not radar.empty:
        table = Table(title="SLA Breach Risk Radar", header_style="bold white on #C0392B")
        for column in ["key", "priority", "status", "runway_hours", "breach_probability", "risk_level"]:
            table.add_column(column.replace("_", " ").title(), justify="right")
        for _, row in radar.head(top).iterrows():
            colour = {
                "Critical": "red",
                "High": "dark_orange",
                "Medium": "yellow",
                "Low": "green",
            }.get(str(row["risk_level"]), "white")
            table.add_row(
                str(row["key"]),
                str(row["priority"]),
                str(row["status"]),
                f"{row['runway_hours']:.1f}",
                f"{row['breach_probability']:.1f}%",
                f"[{colour}]{row['risk_level']}[/{colour}]",
            )
        console.print(table)
        console.print(
            Panel(
                "Escalation guidance is generated by the SLA breach forecaster. "
                "Review before acting.",
                title="Note",
                border_style="yellow",
            )
        )


@main.command()
@click.option(
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional raw Jira dump JSON; omit to use mock/live extraction.",
)
@click.option("--excel/--no-excel", default=False, help="Generate the Excel workbook.")
@click.option("--pptx/--no-pptx", default=False, help="Generate the PowerPoint deck.")
@click.option("--confluence/--no-confluence", default=False, help="Generate Confluence exports.")
@click.option("--all", "generate_all", is_flag=True, help="Generate every report type.")
@click.option("--project", default=None, help="Jira project key.")
@click.option("--days", default=None, type=int, help="Look-back window in days.")
def report(
    input_path: Path | None,
    excel: bool,
    pptx: bool,
    confluence: bool,
    generate_all: bool,
    project: str | None,
    days: int | None,
) -> None:
    """Auto-generate Excel, PowerPoint and Confluence deliverables."""

    config = get_config()
    project = project or config.project.key
    days = days or config.project.default_days

    if generate_all or not any([excel, pptx, confluence]):
        excel = pptx = confluence = True

    console.rule("[bold]SupportPulse report[/bold]")
    tickets = _resolve_tickets(str(input_path) if input_path else None, project, days, None)
    policy = load_policy()
    metrics = MetricsEngine(policy).compute(tickets)
    output_dir = resolve_output_dir()

    generated: dict[str, Path] = {}
    if excel:
        with console.status("Building Excel workbook…"):
            generated["excel"] = ExcelReporter(policy, output_dir).build(tickets, metrics)
    if pptx:
        with console.status("Building PowerPoint deck…"):
            generated["pptx"] = PptxReporter(policy, output_dir).build(tickets, metrics)
    if confluence:
        with console.status("Building Confluence exports…"):
            generated.update(
                ConfluenceExporter(policy, output_dir).export(tickets, metrics)
            )

    table = Table(title="Generated reports", header_style="bold white on #1F3864")
    table.add_column("Artefact", style="cyan")
    table.add_column("Path", style="bold")
    table.add_column("Size", justify="right")
    for name, path in generated.items():
        size = path.stat().st_size if path.exists() else 0
        table.add_row(name, str(path), f"{size / 1024:.1f} KB")
    console.print(table)
    console.print("[green]✔[/green] Reporting complete.")


@main.command()
@click.option("--port", default=8501, type=int, help="Streamlit server port.")
def dashboard(port: int) -> None:
    """Launch the Streamlit visual-management dashboard."""

    import subprocess
    import sys

    app_path = Path(__file__).parent / "dashboard" / "app.py"
    console.print(f"[green]Launching[/green] Streamlit dashboard on port {port}…")
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port)],
        check=False,
    )


def _ticket_to_raw(ticket: JiraTicket) -> dict[str, Any]:
    """Serialise a ticket back into a Jira-like payload for round-tripping."""

    return {
        "key": ticket.key,
        "fields": {
            "summary": ticket.summary,
            "priority": {"name": ticket.priority.value},
            "status": {"name": ticket.status.value},
            "created": ticket.created_at.isoformat(),
            "resolutiondate": ticket.effective_resolved_at.isoformat()
            if ticket.effective_resolved_at
            else None,
            "assignee": {"displayName": ticket.assignee} if ticket.assignee else None,
            "components": [{"name": ticket.component}] if ticket.component else [],
            "labels": ticket.labels,
        },
        "changelog": {
            "histories": [
                {
                    "created": transition.at.isoformat(),
                    "author": {"displayName": transition.author or "Automation"},
                    "items": [
                        {
                            "field": "status",
                            "fromString": transition.from_status,
                            "toString": transition.to_status,
                        },
                        {
                            "field": "assignee",
                            "fromString": "",
                            "toString": transition.tier or "",
                        },
                    ],
                }
                for transition in ticket.transitions
            ]
        },
    }


if __name__ == "__main__":  # pragma: no cover
    main()
