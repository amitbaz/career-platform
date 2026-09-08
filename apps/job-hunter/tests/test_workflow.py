from pathlib import Path

import yaml


# Workflows live at the monorepo root, three levels above apps/job-hunter/tests.
REPO_ROOT = Path(__file__).resolve().parents[3]
DAILY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "job-hunter-daily.yml"
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


def _load_workflow_steps():
    workflow = yaml.safe_load(DAILY_WORKFLOW.read_text())
    job = workflow["jobs"]["run"]
    gmail_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Sync Gmail intelligence"
    )
    run_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run job hunter"
    )
    return job, gmail_step, run_step


def test_gmail_sync_step_is_bounded_and_fail_open():
    job, gmail_step, _ = _load_workflow_steps()

    assert job["timeout-minutes"] == 60
    assert gmail_step["timeout-minutes"] == 20
    assert gmail_step["continue-on-error"] is True


def test_both_ai_steps_carry_the_optional_quota_overrides():
    """The three limits are optional overrides (#73), not required setup.

    They are still plumbed through, so an operator whose project limits differ
    from the published ones can set them; an unset repository variable expands
    to an empty string and config treats that as absent.
    """
    _, gmail_step, run_step = _load_workflow_steps()

    expected_quota_vars = {
        "GEMINI_FREE_RPM": "${{ vars.GEMINI_FREE_RPM }}",
        "GEMINI_FREE_TPM": "${{ vars.GEMINI_FREE_TPM }}",
        "GEMINI_FREE_RPD": "${{ vars.GEMINI_FREE_RPD }}",
    }

    for step in (gmail_step, run_step):
        env = step["env"]
        for key, expected_value in expected_quota_vars.items():
            assert env[key] == expected_value


def test_no_workflow_step_sets_a_run_id_for_ai_accounting():
    """`GEMINI_RUN_ID` is gone: the ledger is per user, not per run (#73)."""
    for workflow_path in (DAILY_WORKFLOW, COVER_LETTER_WORKFLOW):
        workflow = yaml.safe_load(workflow_path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert "GEMINI_RUN_ID" not in (step.get("env") or {})


def test_user_runtime_secrets_are_not_injected_into_workflows():
    for workflow_path in (DAILY_WORKFLOW, COVER_LETTER_WORKFLOW):
        workflow = yaml.safe_load(workflow_path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert USER_RUNTIME_SECRET_NAMES.isdisjoint(
                    (step.get("env") or {}).keys()
                )


def test_run_step_carries_brave_monthly_budget():
    _, _, run_step = _load_workflow_steps()

    assert run_step["env"]["BRAVE_MONTHLY_QUERY_LIMIT"] == (
        "${{ vars.BRAVE_MONTHLY_QUERY_LIMIT || '250' }}"
    )
