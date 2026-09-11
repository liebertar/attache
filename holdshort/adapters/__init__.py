"""The only code that touches the world. Imported by holdshort.runtime.commit and nothing else."""

from holdshort.adapters.fleet_sim import FleetSimAdapter

__all__ = ["FleetSimAdapter", "build"]


def build(kind: str, **kwargs):
    """Which world the runtime is wired to. Everything above this line stays the same."""
    if kind == "mavlink":
        from holdshort.adapters.mavlink_fleet import from_env

        return from_env()
    if kind == "flockwave":
        from holdshort.adapters.flockwave import from_env

        return from_env()
    if kind == "composite":
        # 시뮬레이터가 네 대의 기록의 세계이고, 그중 한 대는 진짜 PX4 도 같이 납니다.
        from holdshort.adapters.composite import from_env

        return from_env(kwargs["sim_url"], world=kwargs.get("world", "guarded"),
                        journal_path=kwargs.get("journal_path"))
    return FleetSimAdapter(kwargs["sim_url"], world=kwargs.get("world", "guarded"))
