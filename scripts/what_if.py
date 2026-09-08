#!/usr/bin/env python3
"""Ask what a rule would have done, before you make it real.

    python3 scripts/what_if.py --ledger .run/ledger.jsonl \
        --forbid-action fast_charge --model robotaxi-v3

    python3 scripts/what_if.py --ledger .run/ledger.jsonl --per-asset 100
"""

import argparse

from attache.core import config as config_module
from attache.core.config import Policy
from attache.runtime.replay import replay


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=".run/ledger.jsonl")
    parser.add_argument("--config", default="configs/fleet.yaml")
    parser.add_argument("--forbid-action")
    parser.add_argument("--forbid-resource")
    parser.add_argument("--model", help="이 기종에만 적용")
    parser.add_argument("--per-asset", type=float)
    parser.add_argument("--fleet", type=float)
    args = parser.parse_args()

    fleet = config_module.load(args.config)
    authority = fleet.authority
    if args.per_asset is not None:
        authority.per_asset_usd = args.per_asset
    if args.fleet is not None:
        authority.fleet_usd = args.fleet

    policies = list(fleet.policies)
    if args.forbid_action or args.forbid_resource:
        policies.append(Policy(
            id="what-if", reason="검토 중인 규칙",
            forbid_action=args.forbid_action,
            forbid_resource=args.forbid_resource,
            applies_to={"model": args.model} if args.model else {},
        ))

    telemetry = {}
    if args.model:
        telemetry = {}  # 기종은 아래에서 모든 자산에 적용합니다

    result = replay(args.ledger, authority, policies,
                    telemetry={a: {"model": args.model} for a in _assets(args.ledger)}
                    if args.model else None)

    print("이 규칙이 지난 기록에 무슨 일을 했을까\n")
    for key, value in result.summary().items():
        print(f"  {key:14} {value}")
    for label, changes in (("새로 거부됨", result.newly_denied),
                           ("새로 사람에게", result.newly_human),
                           ("새로 허용됨", result.newly_allowed)):
        if not changes:
            continue
        print(f"\n{label}:")
        for change in changes[:10]:
            print(f"  {change.asset_id:8} {change.action:18} ${change.cost_usd:>5.0f}"
                  f"  {change.was} → {change.now}")
            print(f"           {change.reason}")
    return 0


def _assets(ledger_path: str) -> set:
    from attache.runtime.replay import read_commits

    return {e["proposal"]["asset_id"] for e in read_commits(ledger_path)}


if __name__ == "__main__":
    raise SystemExit(main())
