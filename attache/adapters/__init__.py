"""The only code that touches the world. Imported by attache.runtime.commit and nothing else."""

from attache.adapters.fleet_sim import FleetSimAdapter

__all__ = ["FleetSimAdapter", "build"]


def build(kind: str, **kwargs):
    """Which world the runtime is wired to. Everything above this line stays the same."""
    if kind == "mavlink":
        from attache.adapters.mavlink_fleet import from_env

        return from_env()
    return FleetSimAdapter(kwargs["sim_url"], world=kwargs.get("world", "guarded"))
