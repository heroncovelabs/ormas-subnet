"""Test-session git identity.

The skeleton clones into fresh workdirs, and a fresh clone has no user.name /
user.email; CI runners have no global identity either. A real miner supplies
its own identity in its environment — the tests do the same here.
"""
from __future__ import annotations

import os

import pytest


# HS-X1 (1ed75bcc): names grounded in provider adapters, runner auth and
# docs/ops/SECOND_ADMIN.md. Keep this list identical in both suite roots:
# public_subnet must also run standalone, without importing tensorbox_spec.
# Do not match generic *_TOKEN / *_SECRET: token budgets, sweep settings,
# IDs, endpoints and credential-file paths are non-secret configuration.
_SCRUBBED_CREDENTIAL_ENV = (
    "AIDER_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CHUTES_API_KEY",
    "CODEX_API_KEY",
    "CURSOR_API_KEY",
    "DAYTONA_API_KEY",
    "DITTO_API_KEY",
    "ENGY_API_KEY",
    "ENGY_READ_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROK_API_KEY",
    "GROK_BUILD_API_KEY",
    "GROK_KEY",
    "HEYDITTO_API_KEY",
    "HF_TOKEN",
    "KIMI_API_KEY",
    "LC_OPENROUTER_BACKUP_TOKEN",
    "LLM_API_KEY",
    "MOONSHOT_API_KEY",
    "MORPH_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "PHALA_CLOUD_API_KEY",
    "PROVIDER_API_KEY",
    "SAYGM_API_KEY",
    "TAOSTATS_API_KEY",
    "TARGON_API_KEY",
    "XAI_API_KEY",
    "X_AI_API_KEY",
    "AZURE_DEVOPS_TOKEN",
    "BITBUCKET_TOKEN",
    "FLY_API_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_CLIENT_SECRET",
    "GITHUB_APP_PRIVATE_KEY",
    "GITLAB_TOKEN",
    "OUTCOMES_GITHUB_VISIBILITY_TOKEN",
    "VERCEL_TOKEN",
    "AUTH_GITHUB_SECRET",
    "AUTH_GOOGLE_SECRET",
    "AUTH_SECRET",
    "BUZZ_PRIVATE_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "DSTACK_AWS_ACCESS_KEY_ID",
    "DSTACK_AWS_SECRET_ACCESS_KEY",
    "DSTACK_AWS_SESSION_TOKEN",
    "DSTACK_DOCKER_PASSWORD",
    "HIPPIUS_ACCESS_KEY",
    "HIPPIUS_ACCESS_KEY_ID",
    "HIPPIUS_SECRET_ACCESS_KEY",
    "HIPPIUS_SECRET_KEY",
    "RESEND_API_KEY",
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_WEBHOOK_SECRET_TEST",
    "TENSORBOX_SPEC_API_TOKEN",
    "TENSORBOX_SPEC_DB_AUTH_TOKEN",
    "TENSORBOX_SPEC_DB_TOKEN",
    "TENSORBOX_SPEC_DEPLOY_KEY_MASTER",
    "TENSORBOX_SPEC_GATEWAY_KEYS",
    "TENSORBOX_SPEC_GATEWAY_INTERNAL_SECRET",
    "TENSORBOX_SPEC_GATEWAY_INTERNAL_SECRET_NEXT",
    "TENSORBOX_SPEC_OUTCOMES_DB_AUTH_TOKEN",
    "TENSORBOX_SPEC_OUTCOMES_QUOTE_SECRET",
    "TENSORBOX_SPEC_OUTCOMES_WEBHOOK_SECRET",
    "TENSORBOX_SPEC_PLAN_REVIEW_SIGNING_KEY",
    "TENSORBOX_SPEC_TBWEB_VERIFY_SECRET",
    "VITE_TENSORBOX_API_TOKEN",
    "ORMAS_API_KEY",
    "ORMAS_EMPLOYEE_API_KEY",
    "ORMAS_GATE12_LEASE_TOKEN",
    "ORMAS_GATEWAY_API_KEY",
    "ORMAS_GATEWAY_INTERNAL_SECRET",
    "ORMAS_GATEWAY_VERIFY_SECRET",
    "ORMAS_INFERENCE_TOKEN",
    "ORMAS_JOB_PROXY_TOKEN",
    "ORMAS_MINER_TOKEN",
    "ORMAS_PROVIDER_KEY_ENCRYPTION_KEY",
    "ORMAS_RUNNER_GITHUB_TOKEN",
    "ORMAS_RUNNER_TOKEN",
    "ORMAS_TOKEN",
    "ORMAS_VALIDATOR_TOKEN",
    "ORMAS_VERIFY_ORACLE_TOKEN",
    "TENSORBOX_ORMAS_API_KEY",
    "ANTHROPIC_ADMIN_KEY",
    "ORMAS_GATEWAY_KEY",
    "ORMAS_GATEWAY_TEST_KEY",
    "TENSORBOX_SPEC_GATEWAY_KEY",
    "U6_CANARY_SECRET",
)
_CREDENTIAL_ENV_PREFIXES = ("ORMAS_RUNNER_PROVIDER_KEY_",)


@pytest.fixture(autouse=True)
def _scrub_ambient_credentials(monkeypatch):
    """Each test supplies its own fake keys; never inherit operator secrets."""
    # Recompute prefixes per test, including raw env writes by earlier tests.
    names = set(_SCRUBBED_CREDENTIAL_ENV) | {
        name for name in os.environ if name.startswith(_CREDENTIAL_ENV_PREFIXES)
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _git_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "ormas-subnet tests",
        "GIT_AUTHOR_EMAIL": "tests@ormas.invalid",
        "GIT_COMMITTER_NAME": "ormas-subnet tests",
        "GIT_COMMITTER_EMAIL": "tests@ormas.invalid",
    }.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)
