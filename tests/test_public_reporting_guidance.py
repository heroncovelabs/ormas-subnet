"""Public support material does not invite operational identifier disclosure."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SHAPED_JOB = re.compile(r"job_([0-9a-f]{12})")
INTERNAL_CARD_REFERENCE = re.compile(r"\bcard\s+[0-9a-f]{8}\b", re.IGNORECASE)


def test_public_issue_template_treats_capability_and_correlation_ids_as_sensitive() -> None:
    for name in ("bug_report.md", "miner_onboarding.md"):
        template = (ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text().lower()
        assert "lease ids" in template and "do not post" in template
        assert "fine to include" not in template
        assert "ops@ormas.ai" in template


def test_source_comments_use_visibly_synthetic_job_ids() -> None:
    offenders: list[str] = []
    for base in (ROOT / "ormas_subnet", ROOT / "neurons", ROOT / "tests"):
        for path in base.rglob("*.py"):
            for match in PRODUCTION_SHAPED_JOB.finditer(path.read_text()):
                value = match.group(1)
                if len(set(value)) != 1:
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_source_does_not_publish_internal_card_references() -> None:
    offenders: list[str] = []
    for base in (ROOT / "ormas_subnet", ROOT / "neurons", ROOT / "tests"):
        for path in base.rglob("*.py"):
            if INTERNAL_CARD_REFERENCE.search(path.read_text()):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
