"""Desktop mock-resume evidence follows task pages (needs PySide6, skipped without it)."""
import pytest

pytest.importorskip("PySide6")

from synapse.desktop.mock_resume import MAX_EVIDENCE_TASKS, fetch_all_tasks  # noqa: E402


def test_desktop_evidence_follows_every_task_page():
    pages = {None: {"tasks": [{"id": "a"}], "next_cursor": "c1"}, "c1": {"tasks": [{"id": "b"}], "next_cursor": None}}
    calls = []

    def fetch(_state, endpoint):
        calls.append(endpoint)
        cursor = endpoint.split("cursor=")[1] if "cursor=" in endpoint else None
        return pages[cursor]
    assert fetch_all_tasks(fetch, "/s", "/api/v1/runs/r") == {"tasks": [{"id": "a"}, {"id": "b"}]}
    assert calls == ["/api/v1/runs/r/tasks?limit=500", "/api/v1/runs/r/tasks?limit=500&cursor=c1"]


@pytest.mark.parametrize("page", [
    [], {"tasks": None}, {"tasks": [], "next_cursor": "c"}, {"tasks": [{"id": "a"}], "next_cursor": 5},
    {"tasks": [{"id": "a"}], "next_cursor": ""},
])
def test_desktop_evidence_fails_closed_on_bad_pages(page):
    with pytest.raises(ValueError):
        fetch_all_tasks(lambda *_: page, "/s", "/api/v1/runs/r")


def test_desktop_evidence_is_bounded_against_an_endless_daemon():
    calls = []

    def fetch(_state, endpoint):
        calls.append(endpoint)
        return {"tasks": [{"id": str(len(calls))}] * 400, "next_cursor": "same"}
    with pytest.raises(ValueError, match="Too many"):
        fetch_all_tasks(fetch, "/s", "/api/v1/runs/r")
    assert len(calls) == MAX_EVIDENCE_TASKS // 400 + 1
