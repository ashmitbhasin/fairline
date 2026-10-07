"""Session sequence handling, replayed against message shapes observed live on 2026-10-07."""
import json

from tape.recorder_ws import Session


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


class FakeWriter:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


TICKERS = ["KXNFLGAME-26OCT08TBDAL-DAL", "KXNFLGAME-26OCT08TBDAL-TB", "KXNFLTOTAL-26OCT08TBDAL-48"]


def snapshot(sid, seq, ticker="KXNFLGAME-26OCT08TBDAL-DAL"):
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": {"market_ticker": ticker}})


def ok(cid, sid, seq):
    return json.dumps({"type": "ok", "id": cid, "sid": sid, "seq": seq, "msg": {"market_tickers": TICKERS}})


async def test_one_subscription_per_channel_with_all_markets():
    ws = FakeWS()
    await Session(ws, TICKERS, FakeWriter()).start()
    assert [m["params"]["channels"] for m in ws.sent] == [["orderbook_delta"], ["trade"]]
    assert all(m["params"]["market_tickers"] == TICKERS for m in ws.sent)


async def test_ok_replies_consume_sequence_numbers_without_false_gaps():
    # Live pattern: ok replies (seq 1, 2), snapshots 3..102, ok replies 103..106, snapshots from 107.
    ws, writer = FakeWS(), FakeWriter()
    s = Session(ws, TICKERS, writer)
    await s.handle(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}))
    await s.handle(ok(3, 1, 1))
    await s.handle(ok(5, 1, 2))
    for seq in range(3, 103):
        await s.handle(snapshot(1, seq))
    for seq in range(103, 107):
        await s.handle(ok(7, 1, seq))
    await s.handle(snapshot(1, 107))
    assert s.stats["gaps"] == 0
    assert not [r for r in writer.records if r.get("type") == "_gap"]
    assert ws.sent == []


async def test_real_gap_resubscribes_every_market():
    ws, writer = FakeWS(), FakeWriter()
    s = Session(ws, TICKERS, writer)
    await s.handle(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}))
    await s.handle(snapshot(1, 1))
    await s.handle(snapshot(1, 3))  # seq 2 lost
    assert s.stats["gaps"] == 1
    assert ws.sent[0] == {"id": 1, "cmd": "unsubscribe", "params": {"sids": [1]}}
    assert ws.sent[1]["cmd"] == "subscribe"
    assert ws.sent[1]["params"] == {"channels": ["orderbook_delta"], "market_tickers": TICKERS}
    gap = [r for r in writer.records if r.get("type") == "_gap"][0]
    assert gap["channel"] == "orderbook_delta" and gap["seq"] == 3
    # Late messages from the dropped subscription are recorded but not re-checked.
    await s.handle(snapshot(1, 9))
    assert s.stats["gaps"] == 1


async def test_trade_channel_gap_resubscribes_trades():
    ws = FakeWS()
    s = Session(ws, TICKERS, FakeWriter())
    await s.handle(json.dumps({"type": "subscribed", "id": 2, "msg": {"channel": "trade", "sid": 2}}))
    await s.handle(json.dumps({"type": "trade", "sid": 2, "seq": 1, "msg": {}}))
    await s.handle(json.dumps({"type": "trade", "sid": 2, "seq": 4, "msg": {}}))
    assert ws.sent[-1]["params"]["channels"] == ["trade"]
