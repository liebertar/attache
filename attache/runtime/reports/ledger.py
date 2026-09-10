"""A flight-by-flight account of what the ledger says, built from the ledger alone.

The ledger is append-only and one filing leaves several lines: an open line, a close line,
and — when the operator re-files the same request after a refusal — another pair. This
module folds those lines back into flights (one per filed request) with the refusals it
took, the approval it got, and what happened to the intent afterwards (conformance,
recall, withdrawal). It reads nothing but the lines: if the report cannot be built from the
ledger, the ledger is not doing its job.
"""

ROUTED = ("reserve_pad", "fly_route")
REFUSAL_KEYS = ("blocked_kind", "blocked_volume", "blocked_asset", "blocked_until_tick")
# 기단 전체에 걸린 규칙. 기체가 아니라 관제탑의 줄이라 비행 밑이 아니라 fleet 절에 접습니다.
HOLD_CODES = ("weather_hold", "weather_hold_lifted", "weather_hold_expired", "weather_hold_closed")


def build_report(entries: list[dict], tick: int, airspace_revision: int,
                 asset: str | None = None) -> dict:
    """원장 줄들 → {generated_tick, airspace_revision, assets:[{asset, flights, advisories}],
    fleet:{weather_holds, incidents}}."""
    closed = [e for e in entries if e.get("outcome") != "pending"]
    flights: dict[str, dict] = {}
    order: list[str] = []
    followups: dict[str, dict] = {}       # 의도 id → {conformance: [...], recalled, withdrawn}
    advisories: dict[str, list[dict]] = {}
    holds: list[dict] = []
    incidents: list[dict] = []

    for entry in closed:
        proposal = entry.get("proposal") or {}
        decision = entry.get("decision") or {}
        context = entry.get("context") or {}
        who = proposal.get("asset_id", "")
        action = proposal.get("action", "")
        code = decision.get("code")
        if code in HOLD_CODES:
            _fold_hold(holds, entry, proposal, decision, context)
            continue
        if code == "incident_keepout":
            detail = decision.get("detail") or {}
            incidents.append({"id": detail.get("incident"), "name": detail.get("name"),
                              "tick": context.get("tick"), "until_tick": detail.get("until_tick"),
                              "radius_m": detail.get("radius_m"), "source": detail.get("source"),
                              "ledger_id": entry.get("id")})
            continue
        if action in ROUTED:
            key = proposal.get("id") or entry.get("id")
            if key not in flights:
                order.append(key)
                flights[key] = _new_flight(who, proposal, context)
            _fold(flights[key], entry, proposal, decision, context)
        elif action == "conformance":
            intent = (proposal.get("params") or {}).get("intent") or context.get("intent_id")
            followups.setdefault(intent, {}).setdefault("conformance", []).append({
                "tick": context.get("tick"),
                "planned_depart_tick": (proposal.get("params") or {}).get("planned_depart_tick"),
                "actual_depart_tick": (proposal.get("params") or {}).get("actual_depart_tick"),
                "ledger_id": entry.get("id")})
        elif decision.get("code") in ("recalled", "withdrawn") and context.get("intent_id"):
            followups.setdefault(context["intent_id"], {})[decision["code"]] = {
                "tick": context.get("tick"), "code": decision.get("code"),
                "policy": decision.get("policy_hit"),
                "for": (decision.get("detail") or {}).get("for"),
                "outcome": entry.get("outcome"), "ledger_id": entry.get("id")}
        elif action == "advisory":
            params = proposal.get("params") or {}
            advisories.setdefault(who, []).append({
                "tick": context.get("tick"), "trigger": params.get("trigger"),
                "chosen": params.get("chosen"), "source": params.get("source"),
                "model": params.get("model"), "summary": params.get("summary"),
                "refusals": len(params.get("refusals") or []),
                "options": [{"id": o.get("id"), "legal": o.get("legal")}
                            for o in params.get("options") or []],
                "ledger_id": entry.get("id")})

    for flight in flights.values():
        after = followups.get(flight["intent"] or "", {})
        flight["conformance"] = after.get("conformance", [])
        flight["recalled"] = after.get("recalled")
        flight["withdrawn"] = after.get("withdrawn")

    by_asset: dict[str, list[dict]] = {}
    for key in order:
        flight = flights[key]
        by_asset.setdefault(flight.pop("asset"), []).append(flight)
    names = sorted(set(by_asset) | set(advisories))
    if asset is not None:
        names = [n for n in names if n == asset]
    return {
        "generated_tick": tick, "airspace_revision": airspace_revision,
        "assets": [{"asset": name,
                    "flights": sorted(by_asset.get(name, []),
                                      key=lambda f: (f["filed_tick"] or 0, f["filed_at"] or 0)),
                    "advisories": advisories.get(name, [])}
                   for name in names],
        "fleet": {"weather_holds": holds, "incidents": incidents},
    }


def _fold_hold(holds: list[dict], entry: dict, proposal: dict, decision: dict,
               context: dict) -> None:
    """기상 대기 한 건 = 연 줄 하나 + (풀린 줄 | 창이 닫힌 줄). 같은 hold id 로 접습니다."""
    params = proposal.get("params") or {}
    detail = decision.get("detail") or {}
    hold = params.get("hold") if isinstance(params.get("hold"), dict) else {}
    hold_id = hold.get("id") or params.get("hold") or detail.get("hold")
    code = decision.get("code")
    if code == "weather_hold":
        holds.append({"id": hold_id, "opened_tick": context.get("tick"),
                      "reason": hold.get("reason") or decision.get("reason"),
                      "until_tick": detail.get("until_tick"), "source": detail.get("source"),
                      "breaches": list(detail.get("breaches") or []), "lifted": None,
                      "expired_tick": None, "ledger_id": entry.get("id")})
        return
    standing = next((h for h in reversed(holds) if h["id"] == hold_id), None)
    if standing is None:
        return
    if code == "weather_hold_lifted":
        standing["lifted"] = {"tick": context.get("tick"), "by": decision.get("approved_by"),
                              "ledger_id": entry.get("id")}
    else:
        # 창이 닫혔거나 판이 바뀌었거나 — 사람 없이 끝난 대기는 둘 다 여기로.
        standing["expired_tick"] = context.get("tick")
        standing["closed_by"] = "round" if code == "weather_hold_closed" else "window"


def _new_flight(asset: str, proposal: dict, context: dict) -> dict:
    return {"asset": asset, "proposal": proposal.get("id"), "intent": None,
            "action": proposal.get("action"), "filed_at": proposal.get("filed_at"),
            "filed_tick": context.get("tick"), "author": proposal.get("author"),
            "drafter": None, "draft_attempts": None, "checks_run": [],
            "refusals": [], "duplicates": 0, "approved": None, "failed": None,
            "conformance": [], "recalled": None, "withdrawn": None}


def _fold(flight: dict, entry: dict, proposal: dict, decision: dict, context: dict) -> None:
    """한 줄을 그 비행에 접어 넣습니다. 누가 그렸는지는 마지막 줄이 말합니다(직선 → 재작성)."""
    params = proposal.get("params") or {}
    if params.get("drafter") is not None:
        flight["drafter"] = params.get("drafter")
    if params.get("draft_attempts") is not None:
        flight["draft_attempts"] = params.get("draft_attempts")
    if context.get("checks_run"):
        flight["checks_run"] = list(context["checks_run"])
    if decision.get("verdict") == "denied":
        if decision.get("code") == "duplicate":
            # 길이 막힌 게 아니라 같은 신청을 두 번 낸 것. 권고의 연속 거절과 같은 기준으로 따로.
            flight["duplicates"] += 1
            return
        flight["refusals"].append({"tick": context.get("tick"), "code": decision.get("code"),
                                   "policy_hit": decision.get("policy_hit"),
                                   **{k: params.get(k) for k in REFUSAL_KEYS},
                                   "ledger_id": entry.get("id")})
        return
    if str(entry.get("outcome") or "").startswith("failed"):
        # 승인은 났는데 조종장치가 못 했습니다(예: 착륙대 위가 아닌데 충전). 거절도 승인도 아닙니다.
        flight["failed"] = {"tick": context.get("tick"), "outcome": entry.get("outcome"),
                            "ledger_id": entry.get("id")}
        return
    if decision.get("committed") or entry.get("outcome") == "done":
        flight["approved"] = {"tick": context.get("tick"), "code": decision.get("code"),
                              "resolution": params.get("resolution"),
                              "holding_for": params.get("holding_for"),
                              "altitude_shift_m": params.get("altitude_shift_m"),
                              "approved_by": decision.get("approved_by"),
                              "outcome": entry.get("outcome"), "ledger_id": entry.get("id")}
        flight["intent"] = context.get("intent_id") or flight["intent"]


def to_markdown(report: dict) -> str:
    """사람이 읽는 표. 비행 하나가 한 줄입니다."""
    lines = [f"# Ledger report — tick {report['generated_tick']}, "
             f"airspace revision {report['airspace_revision']}", "",
             "| asset | flight | filed | author | drafter | tries | checks | refusals | dup "
             "| approved | resolution | conformance | recalled | withdrawn |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for block in report["assets"]:
        for f in block["flights"]:
            refusals = "; ".join(_refusal_word(r) for r in f["refusals"]) or "—"
            approved = f["approved"]
            approved_word = f"tick {approved['tick']} ({approved['code']})" if approved else "—"
            if not approved and f.get("failed"):
                approved_word = f"tick {f['failed']['tick']} ({_cell(f['failed']['outcome'])})"
            resolution = "—"
            if approved and approved.get("resolution"):
                resolution = approved["resolution"]
                if approved.get("holding_for"):
                    resolution += f" ({approved['holding_for']})"
                if approved.get("altitude_shift_m"):
                    resolution += f" (+{approved['altitude_shift_m']:.0f} m)"
            lines.append(
                f"| {block['asset']} | {f['action']} {f['proposal']} | tick {f['filed_tick']} "
                f"| {f['author']} | {f['drafter'] or '—'} | {f['draft_attempts'] or 0} "
                f"| {','.join(f['checks_run']) or '—'} | {_cell(refusals)} "
                f"| {f['duplicates'] or '—'} | {approved_word} "
                f"| {resolution} | {len(f['conformance']) or '—'} "
                f"| {_after_word(f['recalled'])} | {_after_word(f['withdrawn'])} |")
    fleet = report.get("fleet") or {}
    if fleet.get("weather_holds") or fleet.get("incidents"):
        lines += ["", "## Fleet", ""]
        for h in fleet.get("weather_holds") or []:
            ended = (f"lifted tick {h['lifted']['tick']} by {h['lifted']['by']}" if h.get("lifted")
                     else f"closed tick {h['expired_tick']} (round changed)"
                     if h.get("closed_by") == "round"
                     else f"expired tick {h['expired_tick']}" if h.get("expired_tick") else "open")
            lines.append(f"- weather hold · tick {h['opened_tick']} · {_cell(h['reason'])} "
                         f"· until tick {h['until_tick']} · {h['source']} · {ended}")
        for i in fleet.get("incidents") or []:
            lines.append(f"- incident · tick {i['tick']} · {_cell(i['name'])} · {i['radius_m']} m "
                         f"· until tick {i['until_tick']} · {i['source']}")
    advisories = [(block["asset"], a) for block in report["assets"] for a in block["advisories"]]
    if advisories:
        lines += ["", "## Advisories", "", "| asset | tick | trigger | chosen | source | summary |",
                  "|---|---|---|---|---|---|"]
        for asset, a in advisories:
            lines.append(f"| {asset} | {a['tick']} | {a['trigger']} | {a['chosen']} "
                         f"| {a['source']} | {_cell(a['summary'])} |")
    return "\n".join(lines) + "\n"


def _cell(text) -> str:
    """표 한 칸. 모델이 쓴 요약의 '|' 는 칸을 하나 더 만들고 줄바꿈은 행을 깨므로 벗깁니다."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


def _refusal_word(r: dict) -> str:
    what = r.get("blocked_kind") or r.get("policy_hit") or r.get("code") or "?"
    who = r.get("blocked_asset") or r.get("blocked_volume")
    return f"t{r.get('tick')} {what}" + (f":{who}" if who else "")


def _after_word(item: dict | None) -> str:
    if not item:
        return "—"
    return f"tick {item.get('tick')}" + (f" ({item['policy']})" if item.get("policy") else "")
