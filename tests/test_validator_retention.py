"""Completed review clones have no reader: posts use a signed decision, re-reviews clone anew."""

from pathlib import Path
import pytest
from ormas_subnet import validator as v


@pytest.mark.parametrize("decision", ["accept", "reject", "error"])
def test_review_assignment_releases_clone(tmp_path, monkeypatch, decision):
    root = tmp_path / "work"
    work = root / "asgn_abc"

    def clone(self, assignment):
        work.mkdir(parents=True)
        (work / "large").write_text("clone")
        return work

    monkeypatch.setattr(v.ValidatorDaemon, "_clone_with_retry", clone)
    monkeypatch.setattr(v.ValidatorDaemon, "_decide", lambda *a: decision)
    assert v.review_assignment(str(root), {"assignment_id": "asgn_abc"}) == decision
    assert not work.exists(), "completed check clone is not post/re-review evidence"


def test_run_once_releases_clone_even_if_post_fails(tmp_path, monkeypatch):
    work = tmp_path / "asgn_abc"
    work.mkdir()

    class Client:
        def list_assignments(self):
            return [{"assignment_id": "asgn_abc"}]

        def post_decision(self, *a, **kw):
            raise OSError("offline")

    daemon = v.ValidatorDaemon(
        Client(), v.ValidatorConfig(workdir_root=tmp_path), lambda x: "signature"
    )
    monkeypatch.setattr(daemon, "_preface", lambda a: ("review", "digest"))
    monkeypatch.setattr(daemon, "_clone_with_retry", lambda a: work)
    monkeypatch.setattr(daemon, "_decide", lambda *a: "accept")
    with pytest.raises(OSError):
        daemon.run_once()
    assert not work.exists(), "pending post reads only decision/signature"
