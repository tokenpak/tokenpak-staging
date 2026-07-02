"""route command — model routing configuration and status."""

from __future__ import annotations

from types import SimpleNamespace


def run(action: str = "status", raw: bool = False) -> None:
    """Model routing control."""
    from tokenpak._cli_core import cmd_route_list, cmd_route_status

    args = SimpleNamespace(routes=None)
    if action == "list":
        cmd_route_list(args)
    else:
        cmd_route_status(args)


try:
    import click

    @click.group("route")
    def route_cmd():
        """Model routing commands."""
        pass

    @route_cmd.command("status")
    @click.option("--raw", is_flag=True)
    def route_status(raw):
        """Show router status."""
        run(action="status", raw=raw)

    @route_cmd.command("list")
    def route_list():
        """List routing rules."""
        run(action="list")

except ImportError:
    pass
