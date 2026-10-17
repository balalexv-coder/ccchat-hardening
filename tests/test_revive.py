"""A message to a session whose claude died is not typed into nothing: claude is started again first,
and while it cannot be, the message stays queued and the UI is told."""
import asyncio

from backend.session import Session


def _session(tmp_path, alive):
    s = Session({"container": "c", "id": "x", "user": "u"}, tmp_path)
    sent = []

    async def fake_ready():
        return

    async def fake_do_send(text):
        sent.append(text)

    s._wait_ready = fake_ready
    s._do_send = fake_do_send
    s._pane = lambda: "❯ idle prompt"
    s._claude_alive = lambda: alive["v"]
    return s, sent


def test_dead_claude_is_revived_before_delivery(tmp_path):
    alive = {"v": False}
    s, sent = _session(tmp_path, alive)
    revived = []

    def revive():
        revived.append(1)
        alive["v"] = True

    s.on_revive = revive

    async def run():
        s.enqueue_input("hello")
        await asyncio.sleep(0.8)
        assert revived == [1] and sent == ["hello"]

    asyncio.run(run())


def test_message_stays_queued_while_claude_cannot_start(tmp_path):
    alive = {"v": False}
    s, sent = _session(tmp_path, alive)
    s.on_revive = lambda: None
    s.REVIVE_RETRY_S = 0.3
    q = asyncio.Queue()

    async def run():
        s._subscribers.add(q)
        s.enqueue_input("hello")
        await asyncio.sleep(0.6)
        assert sent == [] and s._inq == ["hello"]
        assert (await q.get()) == {"kind": "blocked", "reason": "claude_down"}
        alive["v"] = True                    # claude came back (e.g. a manual restart)
        await asyncio.sleep(0.8)
        assert sent == ["hello"]

    asyncio.run(run())
