"""RealXReader: thread-safe cost recording.

The reader is built once on the main thread but runs inside scheduler worker
threads. It must NOT hold a SQLite connection (cross-thread use raises
ProgrammingError and aborts every monitor/live-reply scan). It opens a fresh
connection from a factory per cost-recording call instead.
"""

from __future__ import annotations

from xgrowth import cost, db
from xgrowth.x_read import RealXReader


def test_record_opens_fresh_connection_per_call(tmp_path):
    dbpath = str(tmp_path / "t.db")
    init = db.connect(dbpath)
    db.init_db(init)
    init.close()

    opened = []

    def factory():
        c = db.connect(dbpath)
        opened.append(c)
        return c

    reader = RealXReader("tok", conn_factory=factory)
    reader._record("user_read", 1)
    reader._record("post_read", 3)

    # Each call opened its OWN connection — nothing is held across calls/threads.
    assert len(opened) == 2
    assert not hasattr(reader, "_conn")

    verify = db.connect(dbpath)
    try:
        assert cost.weekly_spend(verify) > 0  # the cost actually landed
    finally:
        verify.close()


def test_record_without_factory_is_a_safe_noop():
    reader = RealXReader("tok")  # no conn_factory (e.g. cost tracking disabled)
    reader._record("user_read", 1)  # must not raise


def test_zero_count_records_nothing(tmp_path):
    dbpath = str(tmp_path / "t.db")
    init = db.connect(dbpath)
    db.init_db(init)
    init.close()

    opened = []

    def factory():
        c = db.connect(dbpath)
        opened.append(c)
        return c

    reader = RealXReader("tok", conn_factory=factory)
    reader._record("user_read", 0)  # nothing read -> no connection, no row
    assert opened == []
