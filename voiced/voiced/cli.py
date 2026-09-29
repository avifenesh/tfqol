"""voicectl: operator CLI."""
from __future__ import annotations

import os
import signal
import subprocess
import json
import shutil
from pathlib import Path

import click

from .config import LOG_FILE, RUNTIME_DIR, ensure_dirs


PIDFILE = RUNTIME_DIR / "voiced.pid"
MUTE_FILE = RUNTIME_DIR / "voiced.muted"


@click.group()
def main() -> None:
    """voiced operator CLI."""


@main.command()
def mute() -> None:
    """Mute: ignore all speech."""
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
    statefile = RUNTIME_DIR / "state.json"
    if statefile.exists():
        try:
            state = json.loads(statefile.read_text())
            click.echo(f"state: {state.get('state', 'unknown')}; automatic finish after {state.get('finish_pause_ms', 5000) / 1000:g}s")
        except (OSError, ValueError):
            pass


@main.command()
def finish() -> None:
    """Finish the current utterance and run its final correction now."""
    if not _signal_daemon(signal.SIGHUP):
        raise click.ClickException("voiced is not running.")
    click.echo("finish requested")


@main.command(name="copy")
def copy_latest() -> None:
    """Copy the latest draft when automatic editing stopped."""
    file = RUNTIME_DIR / "latest.txt"
    if not file.exists():
        raise click.ClickException("No saved dictation is available yet.")
    text = file.read_text()
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = ["wl-copy", "--type", "text/plain;charset=utf-8"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    else:
        raise click.ClickException(f"Run this command from your desktop terminal. Latest text: {file}")
    result = subprocess.run(command, input=text, text=True, capture_output=True, timeout=5)
    if result.returncode:
        raise click.ClickException(result.stderr.strip() or "Clipboard copy failed.")
    click.echo("Latest dictation copied.")


@main.command()
@click.option("-n", default=50, help="lines to tail")
@click.option("-f", "--follow", is_flag=True, help="Follow new log lines")
def logs(n: int, follow: bool) -> None:
    if not LOG_FILE.exists():
        click.echo("(no log yet)")
        return
    subprocess.run(["tail", "-n", str(n), *(["-f"] if follow else []), str(LOG_FILE)])


def _signal_daemon(sig: int) -> bool:
    if not PIDFILE.exists():
        return False
    fd = None
    try:
        pid = int(PIDFILE.read_text().strip())
        fd = os.pidfd_open(pid)
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if not any(Path(arg.decode(errors="replace")).name == "voiced" for arg in args):
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    main()
