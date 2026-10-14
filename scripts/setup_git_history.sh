#!/usr/bin/env bash
#
# setup_git_history.sh
# --------------------
# Initialise the repository and construct an authentic, everyday commit
# history for SupportPulse-Analytics between 2026-10-01 and 2026-10-15.
#
# Cadence rules:
#   * One commit per active calendar day.
#   * Sundays (2026-10-04 and 2026-10-11) are skipped entirely.
#   * Exactly 13 commits across 13 active days.
#   * Timestamps are expressed in CEST (UTC+02:00).
#
# Usage:  bash scripts/setup_git_history.sh
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the repository root (the directory containing this script's parent).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Author / committer identity
# ---------------------------------------------------------------------------
export GIT_AUTHOR_NAME="Vinothkumar N"
export GIT_AUTHOR_EMAIL="vinothofficial14@gmail.com"
export GIT_COMMITTER_NAME="Vinothkumar N"
export GIT_COMMITTER_EMAIL="vinothofficial14@gmail.com"

# ---------------------------------------------------------------------------
# Repository bootstrap
# ---------------------------------------------------------------------------
if [ ! -d ".git" ]; then
  git init -q
  git symbolic-ref HEAD refs/heads/main
  echo "Initialised empty Git repository in ${REPO_ROOT}/.git"
fi

git config user.name  "${GIT_AUTHOR_NAME}"
git config user.email "${GIT_AUTHOR_EMAIL}"
git config commit.gpgsign false

# ---------------------------------------------------------------------------
# Commit helper — stages the given paths and commits at an exact timestamp.
# ---------------------------------------------------------------------------
commit_on() {
  local when="$1"; shift
  local message="$1"; shift

  git add -- "$@"
  GIT_AUTHOR_DATE="${when}" \
  GIT_COMMITTER_DATE="${when}" \
    git commit -q -m "${message}"
  printf '  %s  %s\n' "${when}" "${message}"
}

echo "Building commit timeline (13 commits, Sundays 04 & 11 Oct skipped)…"

# Day 1 — October 1, 2026 (Thu) 10:30 CEST
commit_on "2026-10-01T10:30:00+0200" \
  "chore: scaffold supportpulse-analytics layout, dependencies, and pyproject.toml" \
  .gitignore pyproject.toml LICENSE

# Day 2 — October 2, 2026 (Fri) 14:15 CEST
commit_on "2026-10-02T14:15:00+0200" \
  "feat(config): implement yaml settings loader and mock jira dataset configuration" \
  config/config.yaml config/sla_policies.yaml \
  src/supportpulse/__init__.py src/supportpulse/config.py

# Day 3 — October 3, 2026 (Sat) 16:45 CEST
commit_on "2026-10-03T16:45:00+0200" \
  "feat(models): define pydantic schemas for jira tickets, changelogs, and sla thresholds" \
  src/supportpulse/models

# October 4, 2026 (Sun) — SUNDAY OFF — 0 commits

# Day 4 — October 5, 2026 (Mon) 11:20 CEST
commit_on "2026-10-05T11:20:00+0200" \
  "feat(extractor): implement jira rest client with jql querying and pagination" \
  src/supportpulse/extractor

# Day 5 — October 6, 2026 (Tue) 15:40 CEST
commit_on "2026-10-06T15:40:00+0200" \
  "feat(analytics): build ticket lifecycle parser and mttr cycle-time calculators" \
  src/supportpulse/analytics/__init__.py \
  src/supportpulse/analytics/metrics_engine.py \
  src/supportpulse/analytics/cycle_times.py

# Day 6 — October 7, 2026 (Wed) 10:15 CEST
commit_on "2026-10-07T10:15:00+0200" \
  "feat(analytics): implement statistical sla breach risk forecasting model" \
  src/supportpulse/analytics/sla_forecaster.py

# Day 7 — October 8, 2026 (Thu) 17:30 CEST
commit_on "2026-10-08T17:30:00+0200" \
  "test: add comprehensive test suite for extraction routines and sla metrics" \
  tests

# Day 8 — October 9, 2026 (Fri) 14:50 CEST
commit_on "2026-10-09T14:50:00+0200" \
  "feat(reports): build automated excel workbook generator with openpyxl heatmaps" \
  src/supportpulse/reporters/__init__.py \
  src/supportpulse/reporters/excel_reporter.py

# Day 9 — October 10, 2026 (Sat) 13:10 CEST
commit_on "2026-10-10T13:10:00+0200" \
  "feat(reports): implement automated powerpoint executive slide deck generator" \
  src/supportpulse/reporters/pptx_reporter.py \
  src/supportpulse/reporters/confluence_exporter.py

# October 11, 2026 (Sun) — SUNDAY OFF — 0 commits

# Day 10 — October 12, 2026 (Mon) 09:45 CEST
commit_on "2026-10-12T09:45:00+0200" \
  "feat(dashboard): construct streamlit visual management dashboard with cfd charts" \
  src/supportpulse/dashboard

# Day 11 — October 13, 2026 (Tue) 16:20 CEST
commit_on "2026-10-13T16:20:00+0200" \
  "feat(cli): bind extraction, analytics, and reporting into unified rich cli" \
  src/supportpulse/cli.py

# Day 12 — October 14, 2026 (Wed) 15:10 CEST
commit_on "2026-10-14T15:10:00+0200" \
  "ci: configure gitlab-ci pipeline for linting, pytest, and docker build" \
  .gitlab-ci.yml Dockerfile docker-compose.yml scripts

# Day 13 — October 15, 2026 (Thu) 17:05 CEST
commit_on "2026-10-15T17:05:00+0200" \
  "release: finalize v1.0.0 documentation, svg screenshots, and public readme" \
  README.md docs examples

echo
echo "Commit history complete:"
git log --pretty=format:'%h  %ad  %s' --date=format-local:'%Y-%m-%d %H:%M' | cat
echo
echo "Total commits: $(git rev-list --count HEAD)"
