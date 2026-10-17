"""Memory facade (delegates to Hermes). Tool registry tests live in test_tools.py."""

from app.hermes.client import HermesReply, ToolActivity
from app.memory.manager import MemoryManager


class MemoryHermes:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def send_message(self, message, on_chunk=None):
        self.prompts.append(message)
        return self.reply


async def test_memory_recall_reads_hermes_files(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("Project Xenio uses ACP\n§\nUser prefers short answers", encoding="utf-8")
    (memories / "USER.md").write_text("Name: Tanishq", encoding="utf-8")
    manager = MemoryManager(MemoryHermes(HermesReply("")), hermes_home=tmp_path)
    everything = await manager.recall()
    assert [e.text for e in everything] == ["Project Xenio uses ACP", "User prefers short answers", "Name: Tanishq"]
    assert [e.store for e in await manager.recall("short")] == ["memory"]


async def test_memory_recall_empty_when_no_files(tmp_path):
    assert await MemoryManager(MemoryHermes(HermesReply("")), hermes_home=tmp_path).recall() == []


async def test_memory_remember_requires_completed_tool_call(tmp_path):
    saved = HermesReply("saved", tool_calls=[ToolActivity("1", title="memory: add", status="completed")])
    hermes = MemoryHermes(saved)
    assert await MemoryManager(hermes, hermes_home=tmp_path).remember("likes tea")
    assert "likes tea" in hermes.prompts[0]
    claimed_only = HermesReply("saved!", tool_calls=[])
    assert not await MemoryManager(MemoryHermes(claimed_only), hermes_home=tmp_path).remember("likes tea")
