"""The only code that touches the world. Imported by attache.runtime.commit and nothing else."""

from attache.adapters.fleet_sim import FleetSimAdapter

__all__ = ["FleetSimAdapter"]
