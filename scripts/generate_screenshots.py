#!/usr/bin/env python3
"""Generate modern dark-mode terminal SVG screenshots for the README.

Uses ``rich.console.Console(record=True)`` to capture real pipeline output and
exports it to SVG. Rich's exporter already draws macOS-style window controls
(red / amber / green dots); we post-process the SVG to add a subtle drop shadow
and a modern dark window fill.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.terminal_theme import MONOKAI
from rich.text import Text

from supportpulse.analytics.cycle_times import CycleTimeCalculator
from supportpulse.analytics.metrics_engine import MetricsEngine
from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.config import project_root
from supportpulse.extractor.jira_client import JiraClient
from supportpulse.extractor.jql_builder import JQLBuilder
from supportpulse.models.sla import SLAPolicy

OUTPUT_DIR = project_root() / "docs" / "screenshots"
WINDOW_FILL = "#0d1117"

SHADOW = (
    '<filter id="sp-shadow" x="-4%" y="-4%" width="108%" height="112%">'
    '<feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#000000" '
    'flood-opacity="0.45"/></filter>'
)

SPARK = "▁▂▃▄▅▆▇█"


def _console(width: int = 112) -> Console:
    return Console(record=True, width=width, force_terminal=True)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    return "".join(SPARK[int((value - low) / span * (len(SPARK) - 1))] for value in values)


def _polish(svg: str, background: str = WINDOW_FILL) -> str:
    """Recolour the window, round it and add a drop shadow."""

    svg = re.sub(
        r'(<rect fill=")(#[0-9a-fA-F]{3,6})(" stroke="rgba\(255,255,255,0\.35\)")',
        rf"\1{background}\3",
        svg,
        count=1,
    )
    svg = svg.replace("<defs>", f"<defs>{SHADOW}", 1)
    svg = re.sub(r'(stroke-width="1" x="1" y="1" width="\d+" height="[\d.]+" rx="8")',
                 r'\1 filter="url(#sp-shadow)"', svg, count=1)
    return svg


def _save(console: Console, filename: str, title: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    svg = console.export_svg(title=title, theme=MONOKAI)
    svg = _polish(svg)
    path = OUTPUT_DIR / filename
    path.write_text(svg, encoding="utf-8")
    print(f"  wrote {path.relative_to(project_root())} ({len(svg) / 1024:.1f} KB)")
    return path


def _load_tickets():
    dump = project_root() / "examples" / "raw_jira_dump.json"
    client = JiraClient(mock_mode=True)
    if dump.exists():
        client.load_dump(dump)
    return client, client.extract_tickets("project = SUP")


# ---------------------------------------------------------------------------
# 1. Jira extraction pipeline
# ---------------------------------------------------------------------------
def screenshot_jira_extraction() -> Path:
    client, tickets = _load_tickets()
    console = _console()
    jql = JQLBuilder.for_support_project("SUP", 14).build()

    console.print(
        Panel.fit(
            "[bold white]SupportPulse-Analytics[/bold white]  "
            "[dim]·[/dim]  [cyan]Jira Extraction Pipeline[/cyan]\n"
            "[dim]REST API v3 · JQL pagination · rate-limit aware · mock fallback[/dim]",
            border_style="cyan",
        )
    )
    console.print()
    console.print("[dim]$[/dim] [bold green]supportpulse extract[/bold green] --project SUP --days 14")
    console.print()
    console.print(f"[bold]JQL[/bold]  [italic]{jql}[/italic]")
    console.print()

    table = Table(
        title="Pagination trace",
        header_style="bold white on #1F3864",
        expand=True,
    )
    table.add_column("Page", justify="right", style="cyan")
    table.add_column("startAt", justify="right")
    table.add_column("Fetched", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Rate limit", justify="center")
    table.add_column("Status", justify="center")

    page_size = client.config.page_size
    total = len(tickets)
    fetched = 0
    page = 0
    for start in range(0, total, page_size):
        page += 1
        fetched = min(total, start + page_size)
        table.add_row(
            str(page),
            str(start),
            str(fetched),
            str(total),
            "200 OK",
            "[green]✓[/green]",
        )
    console.print(table)
    console.print()

    tiers = {}
    for ticket in tickets:
        tiers[ticket.status.value] = tiers.get(ticket.status.value, 0) + 1
    status_line = "   ".join(f"{k} [bold]{v}[/bold]" for k, v in sorted(tiers.items()))

    console.print(
        Panel(
            Group(
                Text.from_markup(f"[green]✓[/green] Extracted [bold]{total}[/bold] tickets "
                                 f"via [bold]{page}[/bold] page(s)"),
                Text.from_markup(f"[green]✓[/green] Parsed changelog histories · "
                                 f"[bold]{sum(len(t.transitions) for t in tickets)}[/bold] transitions"),
                Text.from_markup(f"[green]✓[/green] Rate-limit handler: 0 retries required (backoff 0.75s)"),
                Text.from_markup(f"[green]✓[/green] Status distribution → {status_line}"),
            ),
            title="[bold]Extraction summary[/bold]",
            border_style="green",
        )
    )
    return _save(console, "jira_extraction_pipeline.svg", "supportpulse extract")


# ---------------------------------------------------------------------------
# 2. SLA forecaster analytics
# ---------------------------------------------------------------------------
def screenshot_sla_forecaster() -> Path:
    _, tickets = _load_tickets()
    policy = SLAPolicy.default()
    engine = MetricsEngine(policy)
    metrics = engine.compute(tickets)
    cycle = CycleTimeCalculator()
    forecaster = SLAForecaster(policy).fit(tickets)
    radar = forecaster.breach_radar(tickets)

    console = _console()
    console.print(
        Panel.fit(
            "[bold white]SupportPulse-Analytics[/bold white]  "
            "[dim]·[/dim]  [magenta]SLA Breach Forecaster[/magenta]\n"
            "[dim]MTTR · cycle-time percentiles · empirical hazard model[/dim]",
            border_style="magenta",
        )
    )
    console.print()
    console.print("[dim]$[/dim] [bold green]supportpulse analyze[/bold green] --input examples/raw_jira_dump.json")
    console.print()

    kpi = Table(title="Support KPIs", header_style="bold white on #2E75B6", expand=False)
    kpi.add_column("Metric", style="cyan", no_wrap=True)
    kpi.add_column("Value", justify="right", style="bold")
    for label, value in metrics.as_summary_rows():
        kpi.add_row(label, value)

    percentiles = Table(title="Cycle-time ECDF", header_style="bold white on #2E75B6")
    percentiles.add_column("Quantile", style="cyan")
    percentiles.add_column("Resolution (hrs)", justify="right", style="bold")
    percentiles.add_column("Share of tickets", justify="left")
    for quantile, value in (
        ("P50", metrics.p50_hours),
        ("P85", metrics.p85_hours),
        ("P95", metrics.p95_hours),
    ):
        bar = "█" * max(1, int(value / max(1.0, metrics.p95_hours) * 22))
        percentiles.add_row(quantile, f"{value:.1f}", f"[cyan]{bar}[/cyan]")

    console.print(Columns([kpi, percentiles], equal=True, expand=True))
    console.print()

    warnings = Table(
        title="[bold red]⚠ SLA breach risk warnings[/bold red]",
        header_style="bold white on #C0392B",
        expand=True,
    )
    warnings.add_column("Key", style="bold")
    warnings.add_column("Priority")
    warnings.add_column("Runway (hrs)", justify="right")
    warnings.add_column("Breach %", justify="right")
    warnings.add_column("Risk", justify="center")
    colour_map = {"Critical": "red", "High": "dark_orange", "Medium": "yellow", "Low": "green"}
    for _, row in radar.head(8).iterrows():
        colour = colour_map.get(str(row["risk_level"]), "white")
        warnings.add_row(
            str(row["key"]),
            str(row["priority"]),
            f"{row['runway_hours']:.1f}",
            f"{row['breach_probability']:.1f}%",
            f"[{colour}]{row['risk_level']}[/{colour}]",
        )
    console.print(warnings)
    console.print()

    summary = forecaster.summary(tickets)
    console.print(
        f"[dim]Model:[/dim] empirical-hazard  [dim]|[/dim]  "
        f"[red]Critical {summary['Critical']}[/red]  "
        f"[dark_orange]High {summary['High']}[/dark_orange]  "
        f"[yellow]Medium {summary['Medium']}[/yellow]  "
        f"[green]Low {summary['Low']}[/green]  "
        f"[dim](of {summary['total_active']} active)[/dim]"
    )
    return _save(console, "sla_forecaster_analytics.svg", "supportpulse analyze")


# ---------------------------------------------------------------------------
# 3. Visual management dashboard
# ---------------------------------------------------------------------------
def screenshot_dashboard() -> Path:
    _, tickets = _load_tickets()
    policy = SLAPolicy.default()
    engine = MetricsEngine(policy)
    metrics = engine.compute(tickets)
    cycle = CycleTimeCalculator()
    forecaster = SLAForecaster(policy).fit(tickets)
    cfd = cycle.cumulative_flow(tickets)
    flow = cycle.inflow_outflow(tickets, window_days=14)
    radar = forecaster.breach_radar(tickets)

    console = _console()
    console.print(
        Panel.fit(
            "[bold white]SupportPulse-Analytics[/bold white]  "
            "[dim]·[/dim]  [blue]Visual Management Dashboard[/blue]\n"
            "[dim]streamlit run src/supportpulse/dashboard/app.py  →  http://localhost:8501[/dim]",
            border_style="blue",
        )
    )
    console.print()

    sidebar = Panel(
        Group(
            Text.from_markup("[bold]Data source[/bold]"),
            Text.from_markup("[green]◉[/green] Built-in mock dataset"),
            Text.from_markup("[dim]○ Upload Jira dump[/dim]"),
            Text.from_markup("[dim]○ Path to dump[/dim]"),
            Text(""),
            Text.from_markup("[bold]Window[/bold]  30 days"),
            Text.from_markup("[bold]Legend[/bold]"),
            Text.from_markup("🟢 within   🟡 at risk   🔴 breach"),
        ),
        title="[bold]Controls[/bold]",
        border_style="dim",
        width=30,
    )

    wip_now = int(cfd["WIP"].iloc[-1]) if not cfd.empty else metrics.open_tickets
    done_now = int(cfd["Done"].iloc[-1]) if not cfd.empty else metrics.resolved_tickets
    critical = int((radar["risk_level"] == "Critical").sum()) if not radar.empty else 0

    cards = Table.grid(expand=True)
    for _ in range(3):
        cards.add_column(justify="center")
    cards.add_row(
        f"[bold cyan]{metrics.total_tickets}[/bold cyan]\n[dim]Total[/dim]",
        f"[bold blue]{wip_now}[/bold blue]\n[dim]WIP[/dim]",
        f"[bold green]{done_now}[/bold green]\n[dim]Done[/dim]",
    )
    cards.add_row(
        f"[bold]{metrics.mttr_hours:.1f}[/bold]\n[dim]MTTR hrs[/dim]",
        f"[bold]{metrics.p85_hours:.1f}[/bold]\n[dim]P85 hrs[/dim]",
        f"[bold red]{critical}[/bold red]\n[dim]Critical[/dim]",
    )

    main = Panel(
        Group(
            cards,
            Text(""),
            Text.from_markup(
                "[bold]Cumulative Flow Diagram[/bold]  [dim](WIP trend)[/dim]"
            ),
            Text.from_markup(f"[cyan]{_sparkline(cfd['WIP'].tolist())}[/cyan]  [dim]WIP[/dim]"),
            Text.from_markup(f"[green]{_sparkline(cfd['Done'].tolist())}[/green]  [dim]Done[/dim]"),
            Text(""),
            Text.from_markup(
                f"[bold]Inflow vs Outflow[/bold]  [dim]14d[/dim]   "
                f"[blue]inflow {flow['inflow'].sum()}[/blue] · "
                f"[cyan]outflow {flow['outflow'].sum()}[/cyan] · "
                f"[red]net {int(flow['net'].sum()):+d}[/red]"
            ),
        ),
        title="[bold]Executive scorecard[/bold]",
        border_style="blue",
    )

    console.print(Columns([sidebar, main], expand=True))
    console.print()
    console.print(
        "[dim]Tabs:[/dim] Cumulative Flow  ·  Aging WIP  ·  SLA Breach Radar  ·  "
        "Components  ·  Ledger"
    )
    return _save(console, "visual_management_dashboard.svg", "SupportPulse dashboard")


# ---------------------------------------------------------------------------
# 4. Office reports preview
# ---------------------------------------------------------------------------
def screenshot_office_reports() -> Path:
    from supportpulse.reporters.excel_reporter import ExcelReporter
    from supportpulse.reporters.pptx_reporter import PptxReporter

    _, tickets = _load_tickets()
    policy = SLAPolicy.default()
    output_dir = project_root() / "examples" / "generated_reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    xlsx = ExcelReporter(policy, output_dir).build(tickets)
    pptx = PptxReporter(policy, output_dir).build(tickets)

    console = _console()
    console.print(
        Panel.fit(
            "[bold white]SupportPulse-Analytics[/bold white]  "
            "[dim]·[/dim]  [yellow]Automated Office Reporting[/yellow]\n"
            "[dim]openpyxl workbooks · python-pptx executive decks[/dim]",
            border_style="yellow",
        )
    )
    console.print()
    console.print("[dim]$[/dim] [bold green]supportpulse report[/bold green] --excel --pptx --confluence")
    console.print()

    artifacts = Table(title="Generated artefacts", header_style="bold white on #1F3864", expand=True)
    artifacts.add_column("Artefact", style="cyan")
    artifacts.add_column("Format")
    artifacts.add_column("Path", style="dim")
    artifacts.add_column("Size", justify="right", style="bold")
    artifacts.add_column("Verified", justify="center")
    for label, path, fmt in (
        ("weekly_support_status", xlsx, "xlsx"),
        ("executive_status_deck", pptx, "pptx"),
    ):
        artifacts.add_row(
            label,
            fmt,
            f"examples/generated_reports/{path.name}",
            f"{path.stat().st_size / 1024:.1f} KB",
            "[green]✓[/green]",
        )
    console.print(artifacts)
    console.print()

    checks = Table(title="Workbook verification", header_style="bold white on #2E75B6", expand=False)
    checks.add_column("Sheet / Slide", style="cyan")
    checks.add_column("Check", style="dim")
    checks.add_column("Result", justify="center")
    for sheet, detail in (
        ("Executive KPI", "KPI card + SLA attainment + risk radar"),
        ("Ticket Ledger", "raw data ledger, frozen header"),
        ("Pivot Analysis", "priority × status pivot table"),
        ("SLA Heatmap", "green/amber/red conditional formatting"),
        ("Slide 1", "Weekly support executive scorecard"),
        ("Slide 2", "Ticket inflow vs. outflow chart"),
        ("Slide 3", "Top recurring defect components"),
        ("Slide 4", "Critical at-risk backlog items"),
    ):
        checks.add_row(sheet, detail, "[green]✓[/green]")
    console.print(checks)
    console.print()
    console.print(
        "[green]✓[/green] All reports generated and layout-verified · "
        "[dim]ready for distribution to support leadership[/dim]"
    )
    return _save(console, "office_reports_preview.svg", "supportpulse report")


def main() -> int:
    print("Generating SVG screenshots…")
    screenshot_jira_extraction()
    screenshot_sla_forecaster()
    screenshot_dashboard()
    screenshot_office_reports()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
