"""SLA policy models and YAML loader."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from supportpulse.models.ticket import Priority


class SLATarget(BaseModel):
    """Response / resolution / escalation targets for one priority."""

    priority: Priority
    response_hours: float = Field(gt=0)
    resolution_hours: float = Field(gt=0)
    escalation_hours: float | None = Field(default=None, gt=0)

    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority(cls, value: object) -> Priority:
        return Priority.from_any(value)

    def model_post_init(self, _context: object) -> None:  # noqa: D105
        if self.escalation_hours is None:
            object.__setattr__(self, "escalation_hours", round(self.resolution_hours * 0.66, 4))


class SLAPolicy(BaseModel):
    """A complete SLA policy across all priorities."""

    name: str = "Default SLA"
    business_hours_per_day: float = 8.0
    business_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    targets: dict[str, SLATarget] = Field(default_factory=dict)

    def target_for(self, priority: Priority | str) -> SLATarget:
        """Return the SLA target for *priority*, defaulting to P3."""

        prio = Priority.from_any(priority)
        if prio.value in self.targets:
            return self.targets[prio.value]
        for target in self.targets.values():
            if target.priority == prio:
                return target
        # Sensible fallback so forecasting never crashes on a missing policy.
        defaults = {
            Priority.P1: 8.0,
            Priority.P2: 24.0,
            Priority.P3: 56.0,
            Priority.P4: 120.0,
        }
        return SLATarget(
            priority=prio,
            response_hours=max(1.0, defaults[prio] / 8.0),
            resolution_hours=defaults[prio],
        )

    @property
    def resolution_targets(self) -> dict[str, float]:
        """Map of priority value -> resolution target in business hours."""

        return {key: target.resolution_hours for key, target in self.targets.items()}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SLAPolicy":
        """Load a policy from a YAML document."""

        with Path(path).open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        raw_targets = raw.pop("targets", {}) or {}
        targets = {
            key: SLATarget(priority=key, **value) for key, value in raw_targets.items()
        }
        return cls(targets=targets, **raw)

    @classmethod
    def default(cls) -> "SLAPolicy":
        """Return a built-in policy mirroring ``config/sla_policies.yaml``."""

        return cls(
            name="Enterprise Software Support SLA 2026",
            targets={
                "P1-Critical": SLATarget(
                    priority=Priority.P1, response_hours=1, resolution_hours=8
                ),
                "P2-High": SLATarget(
                    priority=Priority.P2, response_hours=4, resolution_hours=24
                ),
                "P3-Medium": SLATarget(
                    priority=Priority.P3, response_hours=8, resolution_hours=56
                ),
                "P4-Low": SLATarget(
                    priority=Priority.P4, response_hours=16, resolution_hours=120
                ),
            },
        )
