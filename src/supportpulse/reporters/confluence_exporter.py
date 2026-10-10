"""Confluence Storage Format (XHTML) and Markdown knowledge-base exporter."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Iterable

from supportpulse.analytics.metrics_engine import MetricsEngine
from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.config import resolve_output_dir
from supportpulse.models.metrics import SupportMetrics
from supportpulse.models.sla import SLAPolicy
from supportpulse.models.ticket import JiraTicket

RAG_MACRO = {
    "Green": "Green",
    "Amber": "Yellow",
    "Red": "Red",
}

RAG_EMOJI = {"Green": "🟢", "Amber": "🟡", "Red": "🔴"}


class ConfluenceExporter:
    """Renders the weekly support status page for Confluence and Markdown."""

    def __init__(
        self,
        sla_policy: SLAPolicy | None = None,
        output_dir: str | Path | None = None,
        hours_per_day: float = 8.0,
        space_key: str = "SUPPORT",
    ) -> None:
        self.policy = sla_policy or SLAPolicy.default()
        self.engine = MetricsEngine(self.policy, hours_per_day=hours_per_day)
        self.forecaster = SLAForecaster(self.policy, hours_per_day=hours_per_day)
        self.output_dir = Path(output_dir) if output_dir else resolve_output_dir()
        self.space_key = space_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def export(
        self,
        tickets: Iterable[JiraTicket],
        metrics: SupportMetrics | None = None,
        output_dir: str | Path | None = None,
    ) -> dict[str, Path]:
        """Write both the XHTML and Markdown artefacts; return their paths."""

        tickets = list(tickets)
        metrics = metrics or self.engine.compute(tickets)
        self.forecaster.fit(tickets)
        target = Path(output_dir) if output_dir else self.output_dir
        target.mkdir(parents=True, exist_ok=True)

        storage_path = target / "weekly_support_status_confluence.xhtml"
        markdown_path = target / "weekly_support_status.md"
        storage_path.write_text(self.to_storage_format(tickets, metrics), encoding="utf-8")
        markdown_path.write_text(self.to_markdown(tickets, metrics), encoding="utf-8")
        return {"confluence": storage_path, "markdown": markdown_path}

    # ------------------------------------------------------------------
    # Confluence Storage Format
    # ------------------------------------------------------------------
    def to_storage_format(
        self, tickets: Iterable[JiraTicket], metrics: SupportMetrics | None = None
    ) -> str:
        tickets = list(tickets)
        metrics = metrics or self.engine.compute(tickets)
        self.forecaster.fit(tickets)
        compliance = self.engine.sla_compliance_table(tickets)
        radar = self.forecaster.breach_radar(tickets)
        hotspots = self.engine.component_hotspots(tickets, top_n=6)

        parts: list[str] = []
        parts.append(
            '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
            f"<p><strong>SupportPulse weekly status</strong> — generated "
            f"{escape(metrics.generated_at.strftime('%d %b %Y %H:%M UTC'))} for space "
            f"{escape(self.space_key)}.</p></ac:rich-text-body></ac:structured-macro>"
        )

        parts.append("<h2>Executive KPIs</h2>")
        parts.append(self._kpi_table(metrics))

        parts.append("<h2>SLA Attainment by Priority</h2>")
        parts.append(self._compliance_table(compliance))

        parts.append("<h2>Top Defect Components</h2>")
        parts.append(self._hotspot_table(hotspots))

        parts.append("<h2>SLA Breach Risk Radar</h2>")
        if radar.empty:
            parts.append("<p>No active tickets in scope.</p>")
        else:
            at_risk = radar[radar["risk_level"].isin(["Critical", "High"])].head(10)
            parts.append(self._risk_table(at_risk if not at_risk.empty else radar.head(10)))

        parts.append(
            '<ac:structured-macro ac:name="note"><ac:rich-text-body>'
            "<p>Escalation guidance is generated automatically by the SLA breach "
            "forecaster and should be reviewed by the support lead before action.</p>"
            "</ac:rich-text-body></ac:structured-macro>"
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------
    def to_markdown(
        self, tickets: Iterable[JiraTicket], metrics: SupportMetrics | None = None
    ) -> str:
        tickets = list(tickets)
        metrics = metrics or self.engine.compute(tickets)
        self.forecaster.fit(tickets)
        compliance = self.engine.sla_compliance_table(tickets)
        radar = self.forecaster.breach_radar(tickets)
        hotspots = self.engine.component_hotspots(tickets, top_n=6)

        lines: list[str] = []
        lines.append("# SupportPulse — Weekly Support Status")
        lines.append("")
        lines.append(
            f"_Generated {metrics.generated_at.strftime('%d %b %Y %H:%M UTC')} · "
            f"window {metrics.window_days} days · space `{self.space_key}`_"
        )
        lines.append("")

        lines.append("## Executive KPIs")
        lines.append("")
        lines.append("| KPI | Value |")
        lines.append("| --- | --- |")
        for label, value in metrics.as_summary_rows():
            lines.append(f"| {label} | {value} |")
        lines.append("")

        lines.append("## SLA Attainment by Priority")
        lines.append("")
        if compliance.empty:
            lines.append("_No resolved tickets in scope._")
        else:
            lines.append("| Priority | Tickets | Resolved | Target (hrs) | Attainment | Status |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for _, row in compliance.iterrows():
                lines.append(
                    f"| {row['priority']} | {row['tickets']} | {row['resolved']} | "
                    f"{row['sla_target_hours']} | {row['attainment'] * 100:.1f}% | "
                    f"{RAG_EMOJI.get(row['status'], '')} {row['status']} |"
                )
        lines.append("")

        lines.append("## Top Defect Components")
        lines.append("")
        if hotspots.empty:
            lines.append("_No components in scope._")
        else:
            lines.append("| Component | Tickets | MTTR (hrs) | Breach rate |")
            lines.append("| --- | --- | --- | --- |")
            for _, row in hotspots.iterrows():
                lines.append(
                    f"| {row['component']} | {row['tickets']} | {row['mttr_hours']} | "
                    f"{row['breach_rate'] * 100:.1f}% |"
                )
        lines.append("")

        lines.append("## SLA Breach Risk Radar")
        lines.append("")
        if radar.empty:
            lines.append("_No active tickets in scope._")
        else:
            lines.append("| Key | Priority | Status | Runway (hrs) | Breach % | Risk | Recommendation |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for _, row in radar.head(12).iterrows():
                lines.append(
                    f"| {row['key']} | {row['priority']} | {row['status']} | "
                    f"{row['runway_hours']} | {row['breach_probability']}% | "
                    f"{row['risk_level']} | {row['escalation_recommendation']} |"
                )
        lines.append("")
        lines.append("> Escalation guidance is generated by the SLA breach forecaster.")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Table fragments
    # ------------------------------------------------------------------
    def _kpi_table(self, metrics: SupportMetrics) -> str:
        rows = "".join(
            f"<tr><td><strong>{escape(label)}</strong></td><td>{escape(value)}</td></tr>"
            for label, value in metrics.as_summary_rows()
        )
        return (
            '<table><tbody>'
            "<tr><th>KPI</th><th>Value</th></tr>"
            f"{rows}</tbody></table>"
        )

    def _compliance_table(self, compliance) -> str:
        if compliance.empty:
            return "<p>No resolved tickets in scope.</p>"
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(row['priority']))}</td>"
            f"<td>{row['tickets']}</td>"
            f"<td>{row['resolved']}</td>"
            f"<td>{row['sla_target_hours']}</td>"
            f"<td>{row['attainment'] * 100:.1f}%</td>"
            f"{self._status_cell(str(row['status']))}"
            "</tr>"
            for _, row in compliance.iterrows()
        )
        return (
            "<table><tbody>"
            "<tr><th>Priority</th><th>Tickets</th><th>Resolved</th>"
            "<th>Target (hrs)</th><th>Attainment</th><th>Status</th></tr>"
            f"{rows}</tbody></table>"
        )

    def _hotspot_table(self, hotspots) -> str:
        if hotspots.empty:
            return "<p>No components in scope.</p>"
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(row['component']))}</td>"
            f"<td>{row['tickets']}</td>"
            f"<td>{row['mttr_hours']}</td>"
            f"<td>{row['breach_rate'] * 100:.1f}%</td>"
            "</tr>"
            for _, row in hotspots.iterrows()
        )
        return (
            "<table><tbody>"
            "<tr><th>Component</th><th>Tickets</th><th>MTTR (hrs)</th>"
            "<th>Breach rate</th></tr>"
            f"{rows}</tbody></table>"
        )

    def _risk_table(self, radar) -> str:
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(row['key']))}</td>"
            f"<td>{escape(str(row['priority']))}</td>"
            f"<td>{escape(str(row['status']))}</td>"
            f"<td>{row['runway_hours']}</td>"
            f"<td>{row['breach_probability']}%</td>"
            f"{self._status_cell(str(row['risk_level']))}"
            f"<td>{escape(str(row['escalation_recommendation']))}</td>"
            "</tr>"
            for _, row in radar.iterrows()
        )
        return (
            "<table><tbody>"
            "<tr><th>Key</th><th>Priority</th><th>Status</th><th>Runway (hrs)</th>"
            "<th>Breach %</th><th>Risk</th><th>Recommendation</th></tr>"
            f"{rows}</tbody></table>"
        )

    @staticmethod
    def _status_cell(status: str) -> str:
        macro = RAG_MACRO.get(status)
        if macro is None:
            return f"<td>{escape(status)}</td>"
        return (
            "<td>"
            f'<ac:structured-macro ac:name="status">'
            f'<ac:parameter ac:name="colour">{macro}</ac:parameter>'
            f'<ac:parameter ac:name="title">{escape(status)}</ac:parameter>'
            "</ac:structured-macro></td>"
        )
