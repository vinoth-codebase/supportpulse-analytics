"""Jira REST API client with pagination, rate-limit handling and mock mode."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from supportpulse.config import JiraConfig, load_config
from supportpulse.extractor.changelog_parser import ChangelogParser
from supportpulse.models.ticket import JiraTicket, Priority, Status, Transition

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, int | None], None]

COMPONENTS = [
    "Network Stack",
    "Installer",
    "Licensing",
    "RF Measurement",
    "Web UI",
    "Firmware Update",
    "Reporting Engine",
    "Authentication",
    "Signal Analysis",
    "Device Manager",
]

ASSIGNEES: dict[str, list[str]] = {
    "Tier 1": ["Priya Menon", "Arjun Rao", "Sofia Klein"],
    "Tier 2": ["Lukas Berger", "Meera Iyer", "Daniel Fischer"],
    "Development": ["Nikhil Sharma", "Anna Weber", "Tobias Lang"],
}

_PRIORITY_WEIGHTS = [
    (Priority.P1, 0.08),
    (Priority.P2, 0.22),
    (Priority.P3, 0.45),
    (Priority.P4, 0.25),
]

_RESOLUTION_HOURS = {
    Priority.P1: (4.0, 2.2),
    Priority.P2: (18.0, 2.6),
    Priority.P3: (44.0, 2.9),
    Priority.P4: (96.0, 3.2),
}


# ---------------------------------------------------------------------------
# Raw payload -> domain model
# ---------------------------------------------------------------------------
def parse_issue(raw_issue: dict[str, Any], parser: ChangelogParser | None = None) -> JiraTicket:
    """Convert a raw Jira REST issue payload into a :class:`JiraTicket`."""

    parser = parser or ChangelogParser()
    fields = raw_issue.get("fields", raw_issue)
    key = raw_issue.get("key") or fields.get("key") or "UNKNOWN-0"

    priority = (fields.get("priority") or {}).get("name") if isinstance(
        fields.get("priority"), dict
    ) else fields.get("priority")
    status = (fields.get("status") or {}).get("name") if isinstance(
        fields.get("status"), dict
    ) else fields.get("status")
    assignee_raw = fields.get("assignee")
    assignee = (
        assignee_raw.get("displayName")
        if isinstance(assignee_raw, dict)
        else assignee_raw
    )
    components = fields.get("components") or []
    component = None
    if components:
        first = components[0]
        component = first.get("name") if isinstance(first, dict) else str(first)

    transitions = parser.parse_issue(raw_issue)

    return JiraTicket(
        key=key,
        summary=fields.get("summary") or "",
        priority=Priority.from_any(priority),
        status=Status.from_any(status),
        created_at=_to_datetime(fields.get("created")) or datetime.now(timezone.utc),
        resolved_at=_to_datetime(fields.get("resolutiondate")),
        assignee=assignee,
        component=component,
        labels=list(fields.get("labels") or []),
        transitions=transitions,
    )


def _to_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Synthetic dataset (mock mode)
# ---------------------------------------------------------------------------
def _weighted_priority(rng: random.Random) -> Priority:
    threshold = rng.random()
    cumulative = 0.0
    for priority, weight in _PRIORITY_WEIGHTS:
        cumulative += weight
        if threshold <= cumulative:
            return priority
    return Priority.P3


def _log_normal_hours(rng: random.Random, priority: Priority) -> float:
    median, sigma = _RESOLUTION_HOURS[priority]
    value = rng.lognormvariate(0, 1)  # heavy right tail
    return max(0.5, median * value * (sigma / 2.0))


def _snap_to_business(
    moment: datetime, hours_per_day: float = 8.0, day_start: int = 9
) -> datetime:
    """Move *moment* forward to the nearest in-hours weekday timestamp."""

    end_hour = day_start + int(hours_per_day)
    cursor = moment
    for _ in range(14):
        if cursor.weekday() < 5 and day_start <= cursor.hour < end_hour:
            return cursor
        if cursor.weekday() >= 5:
            cursor = (cursor + timedelta(days=7 - cursor.weekday())).replace(
                hour=day_start, minute=0, second=0, microsecond=0
            )
        elif cursor.hour < day_start:
            cursor = cursor.replace(hour=day_start, minute=0, second=0, microsecond=0)
        else:
            cursor = (cursor + timedelta(days=1)).replace(
                hour=day_start, minute=0, second=0, microsecond=0
            )
    return cursor


def generate_mock_issues(
    count: int = 140,
    days: int = 45,
    seed: int = 42,
    reference: datetime | None = None,
) -> list[dict[str, Any]]:
    """Create a deterministic, realistic synthetic Jira dataset."""

    rng = random.Random(seed)
    now = reference or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    issues: list[dict[str, Any]] = []

    for index in range(1, count + 1):
        priority = _weighted_priority(rng)
        created = _snap_to_business(
            start + timedelta(seconds=rng.randint(0, max(1, int((now - start).total_seconds()))))
        )
        age_days = (now - created).total_seconds() / 86400.0
        nominal = _log_normal_hours(rng, priority)

        # ~62% resolved, the rest still in flight (some overrunning their SLA).
        resolved = rng.random() < 0.62 or age_days > 40
        if resolved:
            resolved_at = _snap_to_business(created + timedelta(hours=nominal))
            if resolved_at >= now:
                resolved = False
        if not resolved:
            resolved_at = None

        transitions = _build_transitions(created, resolved_at, rng)
        current_status = transitions[-1].to_status if transitions else Status.OPEN.value
        if resolved_at is not None and current_status not in {
            Status.RESOLVED.value,
            Status.CLOSED.value,
        }:
            current_status = Status.RESOLVED.value

        assignee_tier = rng.choices(
            ["Tier 1", "Tier 2", "Development", None], weights=[0.4, 0.35, 0.15, 0.10]
        )[0]
        assignee = (
            rng.choice(ASSIGNEES[assignee_tier]) if assignee_tier else None
        )

        issue = {
            "id": str(10000 + index),
            "key": f"SUP-{1000 + index}",
            "fields": {
                "summary": _summary(rng, priority),
                "priority": {"name": priority.value},
                "status": {"name": current_status},
                "created": created.isoformat(),
                "resolutiondate": resolved_at.isoformat() if resolved_at else None,
                "assignee": {"displayName": assignee} if assignee else None,
                "components": [{"name": rng.choice(COMPONENTS)}],
                "labels": rng.sample(
                    ["regression", "customer-reported", "escalated", "field-issue"],
                    k=rng.randint(0, 2),
                ),
            },
            "changelog": {"histories": _histories(transitions)},
        }
        issues.append(issue)

    issues.sort(key=lambda item: item["fields"]["created"])
    return issues


def _summary(rng: random.Random, priority: Priority) -> str:
    templates = [
        "Intermittent signal loss during long acquisition runs",
        "Installer fails with error 1603 on Windows 11 24H2",
        "License server not reachable after network change",
        "Report export hangs for datasets over 2 GB",
        "Device not detected on USB 3.0 hub",
        "Firmware update rolls back silently",
        "Web UI login loop with SSO enabled",
        "Measurement drift observed above 40 GHz",
        "Crash on startup after plugin install",
        "API returns 500 on bulk ticket query",
    ]
    return f"[{priority.value.split('-')[0]}] {rng.choice(templates)}"


def _build_transitions(
    created: datetime, resolved_at: datetime | None, rng: random.Random
) -> list[Transition]:
    end = resolved_at or (created + timedelta(hours=rng.uniform(6, 120)))
    span_seconds = max(3600.0, (end - created).total_seconds())
    path: list[str] = [Status.IN_TRIAGE.value, Status.IN_PROGRESS.value]
    if rng.random() < 0.35:
        path.append(
            rng.choice([Status.WAITING_CUSTOMER.value, Status.WAITING_VENDOR.value])
        )
    if rng.random() < 0.30:
        path.append(Status.IN_PROGRESS.value)  # reopen / re-work
    if resolved_at is not None:
        path.append(Status.RESOLVED.value)
        if rng.random() < 0.55:
            path.append(Status.CLOSED.value)

    transitions: list[Transition] = []
    previous = Status.OPEN.value
    for position, status in enumerate(path, start=1):
        fraction = position / (len(path) + 1)
        at = created + timedelta(seconds=span_seconds * fraction * rng.uniform(0.8, 1.15))
        at = min(_snap_to_business(at), end)
        if transitions and at < transitions[-1].at:
            at = transitions[-1].at
        tier = rng.choices(
            ["Tier 1", "Tier 2", "Development"], weights=[0.45, 0.40, 0.15]
        )[0]
        transitions.append(
            Transition(
                from_status=previous,
                to_status=status,
                at=at,
                author=rng.choice(ASSIGNEES[tier]),
                tier=tier,
            )
        )
        previous = status

    transitions.sort(key=lambda t: t.at)
    return transitions


def _histories(transitions: Iterable[Transition]) -> list[dict[str, Any]]:
    histories: list[dict[str, Any]] = []
    for transition in transitions:
        histories.append(
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
        )
    return histories


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class JiraClient:
    """Thin Jira REST client.

    Supports live Jira Cloud/Server instances and a self-contained mock mode
    that needs neither credentials nor network access.
    """

    def __init__(
        self,
        config: JiraConfig | None = None,
        *,
        session: requests.Session | None = None,
        mock_mode: bool | None = None,
        mock_dataset: list[dict[str, Any]] | None = None,
        parser: ChangelogParser | None = None,
    ) -> None:
        self.config = config or load_config().jira
        self.mock_mode = bool(mock_mode) if mock_mode is not None else not bool(
            self.config.api_token
        )
        self.session = session or requests.Session()
        self.parser = parser or ChangelogParser()
        self._mock_dataset = mock_dataset
        self.request_count = 0
        self.page_count = 0

    # ------------------------------------------------------------------
    # Mock dataset helpers
    # ------------------------------------------------------------------
    def load_dump(self, path: str | Path) -> list[dict[str, Any]]:
        """Load a raw Jira dump (JSON) from disk into the mock dataset."""

        import json

        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            issues = payload.get("issues", payload.get("data", []))
        else:
            issues = payload
        self._mock_dataset = list(issues)
        self.mock_mode = True
        return self._mock_dataset

    @property
    def dataset(self) -> list[dict[str, Any]]:
        if self._mock_dataset is None:
            self._mock_dataset = generate_mock_issues()
        return self._mock_dataset

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _auth(self) -> tuple[str, str] | None:
        if self.config.email and self.config.api_token:
            return (self.config.email, self.config.api_token)
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.api_token and not self.config.email:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue an HTTP request with retries and rate-limit handling."""

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                self.request_count += 1
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    auth=self._auth(),
                    timeout=self.config.timeout,
                    verify=self.config.verify_ssl,
                )
            except requests.RequestException as exc:  # pragma: no cover - network
                last_error = exc
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                retry_after = float(
                    response.headers.get("Retry-After", self.config.rate_limit_pause)
                )
                logger.warning("Jira rate limit hit; sleeping %.1fs", retry_after)
                time.sleep(min(retry_after, 30.0))
                continue
            if response.status_code in {500, 502, 503, 504}:
                last_error = RuntimeError(f"Jira transient error {response.status_code}")
                self._sleep_backoff(attempt)
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Jira request failed ({response.status_code}): {response.text[:200]}"
                )
            return response.json()

        raise RuntimeError(f"Jira request exhausted retries: {last_error}")

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self.config.backoff_factor * (2**attempt)
        time.sleep(min(delay, 10.0))

    # ------------------------------------------------------------------
    # Search / pagination
    # ------------------------------------------------------------------
    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int | None = None,
        progress: ProgressCallback | None = None,
        expand: str = "changelog",
    ) -> list[dict[str, Any]]:
        """Execute a JQL search, transparently paging through results."""

        if self.mock_mode:
            return self._mock_search(jql, max_results, progress)

        fields = fields or self.config.fields
        collected: list[dict[str, Any]] = []
        start_at = 0
        page = 0
        total: int | None = None

        while True:
            params = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": self.config.page_size,
                "fields": ",".join(fields),
                "expand": expand,
            }
            url = self.config.base_url.rstrip("/") + self.config.search_path
            payload = self._request("GET", url, params=params)
            page += 1
            self.page_count += 1
            batch = payload.get("issues", [])
            total = payload.get("total", total)
            collected.extend(batch)

            if progress:
                progress(page, len(collected), total)

            if not batch or len(batch) < self.config.page_size:
                break
            if max_results is not None and len(collected) >= max_results:
                collected = collected[:max_results]
                break
            start_at += len(batch)

        return collected

    def _mock_search(
        self,
        jql: str,
        max_results: int | None,
        progress: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        dataset = self.dataset
        page_size = max(1, self.config.page_size)
        collected: list[dict[str, Any]] = []
        total = len(dataset)
        page = 0

        for start in range(0, total, page_size):
            page += 1
            self.page_count += 1
            batch = dataset[start : start + page_size]
            collected.extend(batch)
            if progress:
                progress(page, len(collected), total)
            if max_results is not None and len(collected) >= max_results:
                return collected[:max_results]

        return collected

    def fetch_changelog(self, key: str, max_results: int = 200) -> list[dict[str, Any]]:
        """Fetch the changelog histories for a single issue key."""

        if self.mock_mode:
            for issue in self.dataset:
                if issue.get("key") == key:
                    return list((issue.get("changelog") or {}).get("histories", []))
            return []

        url = f"{self.config.base_url.rstrip('/')}/rest/api/{self.config.api_version}/issue/{key}/changelog"
        payload = self._request("GET", url, params={"maxResults": max_results})
        return list(payload.get("values", payload.get("histories", [])))

    # ------------------------------------------------------------------
    # High-level extraction
    # ------------------------------------------------------------------
    def extract_tickets(
        self,
        jql: str,
        *,
        max_results: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[JiraTicket]:
        """Run a JQL search and normalise the results into tickets."""

        raw_issues = self.search(jql, max_results=max_results, progress=progress)
        tickets: list[JiraTicket] = []
        for raw in raw_issues:
            try:
                tickets.append(parse_issue(raw, self.parser))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Skipping malformed issue %s: %s", raw.get("key"), exc)
        return tickets

    def ping(self) -> bool:
        """Return ``True`` when the client can reach the configured Jira."""

        if self.mock_mode:
            return True
        try:
            url = f"{self.config.base_url.rstrip('/')}/rest/api/{self.config.api_version}/myself"
            self._request("GET", url)
            return True
        except Exception:  # pragma: no cover - network
            return False
