"""voicectl — operator CLI."""
from __future__ import annotations

import os
import signal
import subprocess

import click

from .config import LOG_FILE, RUNTIME_DIR, ensure_dirs


PIDFILE = RUNTIME_DIR / "voiced.pid"
MUTE_FILE = RUNTIME_DIR / "voiced.muted"


@click.group()
def main() -> None:
    """voiced operator CLI."""


@main.command()
def mute() -> None:
    """Mute — ignore all speech."""
    ensure_dirs()
    MUTE_FILE.touch()
    _signal_daemon(signal.SIGUSR1)
    click.echo("muted")


@main.command()
def unmute() -> None:
    """Resume listening."""
    if MUTE_FILE.exists():
        MUTE_FILE.unlink()
    _signal_daemon(signal.SIGUSR2)
    click.echo("unmuted")


@main.command()
def status() -> None:
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"running: pid={pid}")
        except (OSError, ValueError):
            click.echo("stale pidfile; daemon not running")
    else:
        click.echo("no pidfile found")
    click.echo(f"log: {LOG_FILE}")
    if MUTE_FILE.exists():
        click.echo("muted")


@main.command()
@click.option("-n", default=50, help="lines to tail")
def logs(n: int) -> None:
    if not LOG_FILE.exists():
        click.echo("(no log yet)")
        return
    subprocess.run(["tail", "-n", str(n), "-f", str(LOG_FILE)])


def _signal_daemon(sig: int) -> None:
    if not PIDFILE.exists():
        return
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, sig)
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    main()
