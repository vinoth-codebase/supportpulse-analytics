"""Plotly chart builders for the Streamlit visual-management dashboard."""

from __future__ import annotations

from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from supportpulse.models.metrics import SupportMetrics
from supportpulse.models.ticket import Status

# Shared palette -------------------------------------------------------------
NAVY = "#1F3864"
BLUE = "#2E75B6"
TEAL = "#2CA6A4"
AMBER = "#ED7D31"
RED = "#C0392B"
GREEN = "#2E7D32"
GREY = "#8A8A8A"

STATUS_COLORS = {
    Status.OPEN.value: "#B4C7E7",
    Status.IN_TRIAGE.value: "#8FAADC",
    Status.IN_PROGRESS.value: "#2E75B6",
    Status.WAITING_CUSTOMER.value: "#F4B183",
    Status.WAITING_VENDOR.value: "#ED7D31",
    Status.RESOLVED.value: "#A9D18E",
    Status.CLOSED.value: "#548235",
}

RISK_COLORS = {"Low": GREEN, "Medium": AMBER, "High": "#E67E22", "Critical": RED}

_TEMPLATE = "plotly_white"


def _empty(message: str, height: int = 380) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper"
    )
    figure.update_layout(template=_TEMPLATE, height=height)
    return figure


def cumulative_flow_figure(flow: pd.DataFrame) -> go.Figure:
    """Stacked-area Cumulative Flow Diagram."""

    if flow is None or flow.empty:
        return _empty("No ticket history available for a Cumulative Flow Diagram.")

    statuses = [s.value for s in Status if s.value in flow.columns]
    figure = go.Figure()
    for status in statuses:
        figure.add_trace(
            go.Scatter(
                x=flow.index,
                y=flow[status],
                name=status,
                mode="lines",
                stackgroup="one",
                line={"width": 0.5, "color": STATUS_COLORS.get(status, GREY)},
                fillcolor=STATUS_COLORS.get(status, GREY),
            )
        )
    figure.update_layout(
        template=_TEMPLATE,
        height=440,
        title="Cumulative Flow Diagram — backlog stability & WIP",
        legend={"orientation": "h", "y": -0.2},
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        yaxis_title="Tickets",
        xaxis_title="Date",
    )
    return figure


def wip_trend_figure(flow: pd.DataFrame) -> go.Figure:
    """Work-in-progress and completed-work trend lines."""

    if flow is None or flow.empty or "WIP" not in flow.columns:
        return _empty("No WIP trend available.")

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(x=flow.index, y=flow["WIP"], name="WIP", line={"color": BLUE, "width": 3})
    )
    figure.add_trace(
        go.Scatter(
            x=flow.index,
            y=flow["Done"],
            name="Done",
            line={"color": GREEN, "width": 3},
        )
    )
    figure.update_layout(
        template=_TEMPLATE,
        height=320,
        title="Work-in-Progress vs. Completed",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
    )
    return figure


def aging_wip_figure(aging: pd.DataFrame, top_n: int = 20) -> go.Figure:
    """Horizontal bar chart of the oldest open tickets."""

    if aging is None or aging.empty:
        return _empty("No aging work-in-progress — the backlog is clean.")

    data = aging.head(top_n).sort_values("age_hours")
    figure = px.bar(
        data,
        x="age_hours",
        y="key",
        orientation="h",
        color="priority",
        hover_data=["status", "assignee", "component"],
        title="Aging Work-in-Progress — stalled & unassigned tickets",
        color_discrete_sequence=[RED, AMBER, BLUE, TEAL],
    )
    figure.update_layout(
        template=_TEMPLATE,
        height=max(360, 26 * len(data)),
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        xaxis_title="Age (business hours)",
        yaxis_title="",
        legend={"orientation": "h", "y": -0.15},
    )
    return figure


def sla_radar_figure(radar: pd.DataFrame, top_n: int = 25) -> go.Figure:
    """Color-coded breach-probability bar chart for the at-risk backlog."""

    if radar is None or radar.empty:
        return _empty("No active tickets to score for breach risk.")

    data = radar.head(top_n).sort_values("breach_probability")
    figure = px.bar(
        data,
        x="breach_probability",
        y="key",
        orientation="h",
        color="risk_level",
        color_discrete_map=RISK_COLORS,
        hover_data=["priority", "status", "runway_hours"],
        title="SLA Breach Radar — predicted risk by ticket",
    )
    figure.add_vline(x=60, line_dash="dash", line_color=AMBER, annotation_text="Imminent")
    figure.add_vline(x=85, line_dash="dash", line_color=RED, annotation_text="Critical")
    figure.update_layout(
        template=_TEMPLATE,
        height=max(380, 24 * len(data)),
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        xaxis_title="Predicted breach probability (%)",
        yaxis_title="",
        legend={"orientation": "h", "y": -0.15},
    )
    return figure


def inflow_outflow_figure(flow: pd.DataFrame) -> go.Figure:
    """Grouped bars for daily inflow vs outflow plus the backlog line."""

    if flow is None or flow.empty:
        return _empty("No inflow/outflow data in the window.")

    figure = go.Figure()
    figure.add_trace(
        go.Bar(x=flow.index, y=flow["inflow"], name="Inflow", marker_color=BLUE)
    )
    figure.add_trace(
        go.Bar(x=flow.index, y=flow["outflow"], name="Outflow", marker_color=TEAL)
    )
    if "cumulative_backlog" in flow.columns:
        figure.add_trace(
            go.Scatter(
                x=flow.index,
                y=flow["cumulative_backlog"],
                name="Cumulative net backlog",
                yaxis="y2",
                line={"color": RED, "width": 2},
            )
        )
    figure.update_layout(
        template=_TEMPLATE,
        height=380,
        barmode="group",
        title="Ticket Inflow vs. Outflow",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        yaxis={"title": "Tickets / day"},
        yaxis2={
            "title": "Net backlog",
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        },
        legend={"orientation": "h", "y": -0.2},
    )
    return figure


def priority_mix_figure(metrics: SupportMetrics) -> go.Figure:
    """Donut of ticket volume by priority."""

    if not metrics.by_priority:
        return _empty("No priority breakdown available.")

    labels = list(metrics.by_priority.keys())
    values = [metrics.by_priority[key].get("resolved", 0) for key in labels]
    figure = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.55,
            marker={"colors": [RED, AMBER, BLUE, TEAL]},
        )
    )
    figure.update_layout(
        template=_TEMPLATE,
        height=320,
        title="Resolved Volume by Priority",
        margin={"l": 20, "r": 20, "t": 60, "b": 20},
    )
    return figure


def component_bar_figure(hotspots: pd.DataFrame) -> go.Figure:
    """Defect volume by component."""

    if hotspots is None or hotspots.empty:
        return _empty("No component data available.")

    figure = px.bar(
        hotspots.sort_values("tickets"),
        x="tickets",
        y="component",
        orientation="h",
        color="breach_rate",
        color_continuous_scale=["#A9D18E", "#FFEB9C", "#FFC7CE"],
        title="Top Recurring Defect Components",
    )
    figure.update_layout(
        template=_TEMPLATE,
        height=380,
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        yaxis_title="",
        xaxis_title="Tickets",
    )
    return figure


def ecdf_figure(resolution_hours: Iterable[float]) -> go.Figure:
    """ECDF of resolution times with SLO reference lines."""

    values = sorted(v for v in resolution_hours if v is not None and v > 0)
    if not values:
        return _empty("No resolved tickets to build an ECDF.")

    total = len(values)
    y = [(i + 1) / total for i in range(total)]
    figure = go.Figure(
        go.Scatter(x=values, y=y, mode="lines", name="ECDF", line={"color": NAVY, "width": 3})
    )
    for level, colour in ((0.85, AMBER), (0.95, RED)):
        figure.add_hline(
            y=level, line_dash="dot", line_color=colour, annotation_text=f"P{int(level * 100)}"
        )
    figure.update_layout(
        template=_TEMPLATE,
        height=380,
        title="Resolution-time ECDF (empirical SLO setting)",
        xaxis_title="Resolution time (business hours)",
        yaxis_title="Cumulative share",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
    )
    return figure
