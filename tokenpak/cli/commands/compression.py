"""compression command — compression telemetry stats."""

from __future__ import annotations

from types import SimpleNamespace


def run(raw: bool = False) -> None:
    """Print compression telemetry stats."""
    from tokenpak._cli_core import cmd_stats

    cmd_stats(SimpleNamespace(raw=raw))


try:
    import click

    @click.command("compression")
    @click.option("--raw", is_flag=True, help="Output raw JSON")
    def compression_cmd(raw):
        """Show compression pipeline stats."""
        run(raw=raw)

except ImportError:
    pass
