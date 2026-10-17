"""Source awareness: the sources recorded during a turn are shown under the answer."""

import json

from app.hermes.client import HermesReply
from app.orchestrator import Orchestrator
from app.sources import SourceLog, describe_sources


def write(path, *items):
    with open(path, "a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")


def test_since_reads_only_new_lines_and_prefers_pages_that_were_read(tmp_path):
    path = tmp_path / "sources.jsonl"
    log = SourceLog(path)
    assert log.mark() == 0 and log.since(0) == []  # no file yet
    write(path, {"kind": "page_read", "url": "https://old.example", "title": "Old"})
    mark = log.mark()
    write(path,
          {"kind": "search_result", "url": "https://docs.example/a", "title": "A docs", "site": "docs.example"},
          {"kind": "page_read", "url": "https://docs.example/a/", "title": "", "site": "docs.example"},
          {"kind": "search_result", "url": "https://blog.example/b", "title": "B"},
          "not json",
          {"kind": "page_read", "url": ""})
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{broken\n")
    sources = log.since(mark)
    assert [s.url.rstrip("/") for s in sources] == ["https://docs.example/a", "https://blog.example/b"]
    assert sources[0].kind == "page_read" and sources[0].title == "A docs"  # kept the title from the search hit


def test_since_keeps_only_this_sessions_sources(tmp_path):
    path = tmp_path / "sources.jsonl"
    write(path, {"url": "https://mine.example", "session_id": "a"}, {"url": "https://other.example", "session_id": "b"},
          {"url": "https://unlabelled.example"})
    assert [s.url for s in SourceLog(path).since(0, "a")] == ["https://mine.example", "https://unlabelled.example"]
    assert len(SourceLog(path).since(0)) == 3


def test_describe_sources_shows_used_pages_then_falls_back_to_search_hits(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, *({"kind": "search_result", "url": f"https://r{i}.example", "title": f"R{i}"} for i in range(6)))
    lines = describe_sources(SourceLog(path).since(0))
    assert lines[0] == "   sources:" and len(lines) == 4 and "(search result)" in lines[1]
    write(path, {"kind": "page_read", "url": "https://docs.example", "title": "Docs"})
    lines = describe_sources(SourceLog(path).since(0))
    assert len(lines) == 2 and "Docs (read) https://docs.example" in lines[1]
    assert describe_sources([]) == []


class FakeHermes:
    """Stands in for Hermes: 'uses' a web page by appending to the sources file, like the plugin does."""

    session_id = "mine"

    def __init__(self, path):
        self.path = path

    def set_permission_handler(self, handler):
        pass

    async def send_message(self, message, on_chunk=None):
        write(self.path, {"kind": "page_read", "url": "https://developers.strava.com/docs", "title": "Strava API",
                          "site": "developers.strava.com", "session_id": "mine"},
              # live: the user's own XENIO, running at the same time, opened a video during this turn
              {"kind": "opened_for_user", "url": "https://www.youtube.com/watch?v=x", "session_id": "someone-else"})
        return HermesReply(text="The Strava API uses OAuth 2.")


async def test_orchestrator_attaches_the_turns_sources(tmp_path):
    path = tmp_path / "sources.jsonl"
    write(path, {"kind": "page_read", "url": "https://earlier.example", "title": "Earlier turn"})
    orch = Orchestrator(FakeHermes(path), None, tmp_path, sources_file=path)
    response = await orch.process_text("research the Strava API")
    assert [s.url for s in response.sources] == ["https://developers.strava.com/docs"]
    no_file = Orchestrator(FakeHermes(tmp_path / "other.jsonl"), None, tmp_path)
    assert (await no_file.process_text("hi")).sources == []
