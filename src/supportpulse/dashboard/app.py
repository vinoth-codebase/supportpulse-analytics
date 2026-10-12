"""Streamlit visual-management dashboard for SupportPulse-Analytics.

Run with::

    streamlit run src/supportpulse/dashboard/app.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from supportpulse.analytics.cycle_times import CycleTimeCalculator
from supportpulse.analytics.metrics_engine import MetricsEngine
from supportpulse.analytics.sla_forecaster import SLAForecaster
from supportpulse.config import get_config, project_root
from supportpulse.dashboard import charts
from supportpulse.extractor.jira_client import JiraClient
from supportpulse.models.sla import SLAPolicy

st.set_page_config(
    page_title="SupportPulse — Visual Management",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
    .kpi-card {background:#F5F7FB;border-left:5px solid #2E75B6;border-radius:8px;
               padding:14px 18px;margin-bottom:8px;}
    .kpi-value {font-size:26px;font-weight:700;color:#1F3864;}
    .kpi-label {font-size:13px;color:#5A5A5A;text-transform:uppercase;letter-spacing:.5px;}
    .block-container {padding-top:2rem;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_tickets(source: str, payload: str | None, days: int):
    """Load tickets from a mock generator, an uploaded dump or a file path."""

    config = get_config()
    client = JiraClient(config.jira, mock_mode=True)

    if source == "upload" and payload:
        dataset = json.loads(payload)
        issues = dataset.get("issues", dataset) if isinstance(dataset, dict) else dataset
        client._mock_dataset = list(issues)  # noqa: SLF001 - intentional injection
    elif source == "path" and payload:
        client.load_dump(payload)
    else:
        client._mock_dataset = None  # noqa: SLF001

    tickets = client.extract_tickets(f"project = {config.project.key}")
    return tickets


def load_policy() -> SLAPolicy:
    policy_path = project_root() / "config" / "sla_policies.yaml"
    if policy_path.exists():
        try:
            return SLAPolicy.from_yaml(policy_path)
        except Exception:  # pragma: no cover - defensive
            return SLAPolicy.default()
    return SLAPolicy.default()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def sidebar() -> tuple[str, str | None, int]:
    st.sidebar.title("SupportPulse")
    st.sidebar.caption("Jira support pipeline · SLA breach forecaster")

    source = st.sidebar.radio(
        "Data source",
        options=["mock", "upload", "path"],
        format_func=lambda value: {
            "mock": "Built-in mock dataset",
            "upload": "Upload Jira dump (JSON)",
            "path": "Path to dump on disk",
        }[value],
    )

    payload: str | None = None
    if source == "upload":
        uploaded = st.sidebar.file_uploader("Jira dump", type=["json"])
        if uploaded is not None:
            payload = uploaded.read().decode("utf-8")
    elif source == "path":
        default_path = project_root() / "examples" / "raw_jira_dump.json"
        payload = st.sidebar.text_input("Dump path", value=str(default_path))

    days = st.sidebar.slider("Reporting window (days)", 7, 90, 30, step=1)

    st.sidebar.divider()
    st.sidebar.markdown(
        "**Legend**\n\n"
        "- 🟢 Within SLA\n- 🟡 At risk\n- 🔴 Imminent / breached"
    )
    st.sidebar.caption(f"Generated {datetime.now(timezone.utc):%d %b %Y %H:%M UTC}")
    return source, payload, days


def kpi_card(label: str, value: str) -> None:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    source, payload, days = sidebar()
    policy = load_policy()

    st.title("SupportPulse — Visual Management Dashboard")
    st.caption(
        "Cumulative Flow, Aging WIP and SLA Breach Radar across the support tiers."
    )

    try:
        tickets = load_tickets(source, payload, days)
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not load tickets: {exc}")
        return

    if not tickets:
        st.warning("No tickets found for the selected source.")
        return

    engine = MetricsEngine(policy)
    metrics = engine.compute(tickets)
    cycle_times = CycleTimeCalculator()
    forecaster = SLAForecaster(policy).fit(tickets)
    radar = forecaster.breach_radar(tickets)
    aging = cycle_times.aging_wip(tickets)
    cfd = cycle_times.cumulative_flow(tickets)
    flow = cycle_times.inflow_outflow(tickets, window_days=14)
    hotspots = engine.component_hotspots(tickets)

    # KPI row ---------------------------------------------------------------
    columns = st.columns(6)
    with columns[0]:
        kpi_card("Total tickets", f"{metrics.total_tickets}")
    with columns[1]:
        kpi_card("Open / WIP", f"{metrics.open_tickets}")
    with columns[2]:
        kpi_card("MTTR (hrs)", f"{metrics.mttr_hours:.1f}")
    with columns[3]:
        kpi_card("P85 (hrs)", f"{metrics.p85_hours:.1f}")
    with columns[4]:
        kpi_card("FCR rate", f"{metrics.fcr_rate * 100:.1f}%")
    with columns[5]:
        critical = int((radar["risk_level"] == "Critical").sum()) if not radar.empty else 0
        kpi_card("Critical risk", f"{critical}")

    tabs = st.tabs(
        ["Cumulative Flow", "Aging WIP", "SLA Breach Radar", "Components", "Ledger"]
    )

    with tabs[0]:
        st.plotly_chart(charts.cumulative_flow_figure(cfd), use_container_width=True)
        left, right = st.columns(2)
        with left:
            st.plotly_chart(charts.wip_trend_figure(cfd), use_container_width=True)
        with right:
            st.plotly_chart(charts.inflow_outflow_figure(flow), use_container_width=True)

    with tabs[1]:
        st.plotly_chart(charts.aging_wip_figure(aging), use_container_width=True)
        if not aging.empty:
            st.dataframe(aging, use_container_width=True, hide_index=True)
        else:
            st.success("No aging work-in-progress — the backlog is clean.")

    with tabs[2]:
        st.plotly_chart(charts.sla_radar_figure(radar), use_container_width=True)
        if not radar.empty:
            st.dataframe(radar, use_container_width=True, hide_index=True)
        summary = forecaster.summary(tickets)
        st.write(
            {key: value for key, value in summary.items() if key != "total_active"}
        )

    with tabs[3]:
        left, right = st.columns([3, 2])
        with left:
            st.plotly_chart(charts.component_bar_figure(hotspots), use_container_width=True)
        with right:
            st.plotly_chart(charts.priority_mix_figure(metrics), use_container_width=True)
        st.plotly_chart(
            charts.ecdf_figure(
                [t.resolution_hours() for t in tickets if t.resolution_hours()]
            ),
            use_container_width=True,
        )

    with tabs[4]:
        ledger = engine.to_frame(tickets)
        st.dataframe(ledger, use_container_width=True, hide_index=True)
        st.download_button(
            "Download ledger CSV",
            data=ledger.to_csv(index=False).encode("utf-8"),
            file_name="supportpulse_ledger.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
