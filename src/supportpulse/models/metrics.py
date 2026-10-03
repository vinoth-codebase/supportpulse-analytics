"""Aggregate support metrics and SLA breach-risk prediction models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from supportpulse.models.ticket import Priority


class RiskLevel(str, Enum):
    """Bucketed breach-risk severity."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"

    @classmethod
    def from_probability(
        cls,
        probability: float,
        high_threshold: float = 60.0,
        critical_threshold: float = 85.0,
    ) -> "RiskLevel":
        if probability >= critical_threshold:
            return cls.CRITICAL
        if probability >= high_threshold:
            return cls.HIGH
        if probability >= 35.0:
            return cls.MEDIUM
        return cls.LOW


class SupportMetrics(BaseModel):
    """Headline support KPIs for a reporting window."""

    total_tickets: int = 0
    resolved_tickets: int = 0
    open_tickets: int = 0
    mttr_hours: float = 0.0
    mttd_hours: float = 0.0
    first_response_hours: float = 0.0
    fcr_rate: float = 0.0
    breach_rate: float = 0.0
    p50_hours: float = 0.0
    p85_hours: float = 0.0
    p95_hours: float = 0.0
    throughput_per_day: float = 0.0
    inflow_per_day: float = 0.0
    wip: int = 0
    backlog_growth: float = 0.0
    window_days: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    by_priority: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_component: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)

    def as_summary_rows(self) -> list[tuple[str, str]]:
        """Human-readable (label, value) pairs for reports and consoles."""

        return [
            ("Total tickets", f"{self.total_tickets}"),
            ("Resolved", f"{self.resolved_tickets}"),
            ("Open / WIP", f"{self.open_tickets}"),
            ("MTTR (business hrs)", f"{self.mttr_hours:.2f}"),
            ("MTTD (business hrs)", f"{self.mttd_hours:.2f}"),
            ("First response (hrs)", f"{self.first_response_hours:.2f}"),
            ("FCR rate", f"{self.fcr_rate * 100:.1f}%"),
            ("SLA breach rate", f"{self.breach_rate * 100:.1f}%"),
            ("P50 resolution (hrs)", f"{self.p50_hours:.2f}"),
            ("P85 resolution (hrs)", f"{self.p85_hours:.2f}"),
            ("P95 resolution (hrs)", f"{self.p95_hours:.2f}"),
            ("Throughput / day", f"{self.throughput_per_day:.2f}"),
            ("Inflow / day", f"{self.inflow_per_day:.2f}"),
            ("Backlog growth", f"{self.backlog_growth:+.2f}"),
        ]


class BreachRiskPrediction(BaseModel):
    """Predicted SLA breach risk for a single active ticket."""

    key: str
    priority: Priority
    status: str
    age_hours: float
    sla_target_hours: float
    runway_hours: float
    breach_probability: float = Field(ge=0.0, le=100.0)
    risk_level: RiskLevel = RiskLevel.LOW
    escalation_recommendation: str = ""
    current_stage_dwell_hours: float = 0.0
    historical_stage_median_hours: float = 0.0
    sample_size: int = 0
    method: str = "empirical-hazard"

    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority(cls, value: object) -> Priority:
        return Priority.from_any(value)

    @property
    def is_breached(self) -> bool:
        return self.runway_hours <= 0

    def to_row(self) -> dict[str, object]:
        return {
            "key": self.key,
            "priority": self.priority.value,
            "status": self.status,
            "age_hours": round(self.age_hours, 2),
            "sla_target_hours": round(self.sla_target_hours, 2),
            "runway_hours": round(self.runway_hours, 2),
            "breach_probability": round(self.breach_probability, 1),
            "risk_level": self.risk_level.value,
            "escalation_recommendation": self.escalation_recommendation,
        }
