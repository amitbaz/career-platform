from pathlib import Path

import yaml


# Workflows live at the monorepo root, three levels above apps/job-hunter/tests.
REPO_ROOT = Path(__file__).resolve().parents[3]
# The daily pipeline workflow (job-hunter-daily.yml) and the `run` command it
# invoked were retired by #189: crawling, extraction and freshness now run as
# their own Render cron services (render.yaml), and matching/delivery have no
# workflow at all until #260/#261. This is the one workflow #189 left behind.
COVER_LETTER_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "job-hunter-generate-cover-letter.yml"
)

# These four now live in the per-user credential store, so no workflow step may
# hand them to the runner as repository-wide secrets.
USER_RUNTIME_SECRET_NAMES = {
    "GEMINI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "CANDIDATE_PROFILE_B64",
    "COVER_LETTER_TEMPLATE_B64",
}


def test_no_workflow_step_sets_a_run_id_for_ai_accounting():
    """`GEMINI_RUN_ID` is gone: the ledger is per user, not per run (#73)."""
    workflow = yaml.safe_load(COVER_LETTER_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            assert "GEMINI_RUN_ID" not in (step.get("env") or {})


def test_user_runtime_secrets_are_not_injected_into_workflows():
    workflow = yaml.safe_load(COVER_LETTER_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            assert USER_RUNTIME_SECRET_NAMES.isdisjoint(
                (step.get("env") or {}).keys()
            )
