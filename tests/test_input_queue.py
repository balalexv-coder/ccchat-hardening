"""Test the server-side input queue: a message sent while claude is mid-turn must be HELD and
delivered only once the pane is idle, in FIFO order — never typed into a live pane (the bug where
messages sent while busy were silently swallowed)."""
import asyncio

from backend.session import Session


def test_input_queue_holds_until_idle_then_delivers_in_order(tmp_path):
    s = Session({"container": "c", "id": "x", "user": "u"}, tmp_path)
    sent = []
    state = {"busy": True}

    async def fake_ready():
        return

    async def fake_do_send(text):
        sent.append(text)

    s._wait_ready = fake_ready
    s._do_send = fake_do_send
    s._pane = lambda: "● working… esc to interrupt" if state["busy"] else "❯ idle prompt"

    async def run():
        s.enqueue_input("first")
        s.enqueue_input("second")
        await asyncio.sleep(1.2)
        assert sent == [], "must not deliver while claude is busy"
        state["busy"] = False              # turn ends
        await asyncio.sleep(2.5)
        assert sent == ["first", "second"], "both delivered, in order, once idle"

    asyncio.run(run())


def test_input_delivered_immediately_when_idle(tmp_path):
    s = Session({"container": "c", "id": "x", "user": "u"}, tmp_path)
    sent = []

    async def fake_ready():
        return

    async def fake_do_send(text):
        sent.append(text)

    s._wait_ready = fake_ready
    s._do_send = fake_do_send
    s._pane = lambda: "❯ idle prompt"      # already idle: no added latency

    async def run():
        s.enqueue_input("hello")
        await asyncio.sleep(0.8)
        assert sent == ["hello"]

    asyncio.run(run())
