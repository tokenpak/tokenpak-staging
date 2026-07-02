"""vault command — vault index management."""

from __future__ import annotations

from types import SimpleNamespace


def run(action: str = "status", raw: bool = False) -> None:
    """Vault index management."""
    from tokenpak._cli_core import cmd_vault_health

    normalized = "repair" if action in ("repair", "reindex") else "status"
    cmd_vault_health(SimpleNamespace(vault_health_cmd=normalized, json=raw))


try:
    import click

    @click.group("vault")
    def vault_cmd():
        """Vault index management commands."""
        pass

    @vault_cmd.command("status")
    @click.option("--raw", is_flag=True)
    def vault_status(raw):
        """Show vault index status."""
        run(action="status", raw=raw)

    @vault_cmd.command("repair")
    @click.option("--verbose", "-v", is_flag=True)
    def vault_repair(verbose):
        """Repair stale or corrupted vault index entries."""
        run(action="repair")

    @vault_cmd.command("reindex")
    @click.option("--verbose", "-v", is_flag=True)
    def vault_reindex(verbose):
        """Alias for repair."""
        run(action="repair")

except ImportError:
    pass
