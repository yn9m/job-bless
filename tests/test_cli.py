"""CLI entry point behaviour: Ctrl+C must be quiet and must return the prompt."""

import asyncio

import main as cli


def test_ctrl_c_is_not_reported_as_a_crash(monkeypatch, caplog):
    """Ctrl+C is a normal way to stop the server, not a stack trace."""
    exits = []
    monkeypatch.setattr(cli, "force_exit_after", lambda seconds: exits.append(seconds))

    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupted)

    with caplog.at_level("INFO"):
        cli.run()  # must not raise

    assert "Остановлено пользователем." in caplog.text
    assert exits == [cli.EXIT_WATCHDOG_SECONDS]


def test_cancelled_error_is_handled_the_same_way(monkeypatch):
    monkeypatch.setattr(cli, "force_exit_after", lambda seconds: None)

    def cancelled(coro):
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(cli.asyncio, "run", cancelled)
    cli.run()


def test_exit_watchdog_is_a_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, daemon, name):
            started.update(daemon=daemon, name=name)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(cli.threading, "Thread", FakeThread)
    cli.force_exit_after(1.0)

    # A daemon thread cannot keep the process alive if the exit is clean.
    assert started == {"daemon": True, "name": "exit-watchdog", "started": True}
