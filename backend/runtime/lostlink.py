"""A link going quiet and coming back.

Owner of `links`, `_dark`, `_released` and `_link_lock`. _links_snapshot stays here rather
than with the other views because it takes that lock; the link cards live in cards.py under
the one-writer rule for the card triple.
"""

from backend.runtime.intents import Intent, LinkEvent
from shared.models import Decision, Proposal, Verdict


class LostLinkMixin:
    def watch_links(self) -> list[LinkEvent]:
        """Watch the telemetry heartbeat. The world thread (_pull_world) and the harness call it
        every poll."""
        with self._link_lock:
            events = self.links.observe(self.telemetry, self.tick)
        for event in events:
            if event.kind == "lost":
                self._link_lost(event)
            else:
                self._link_restored(event)
        return events

    def _link_lost(self, event: LinkEvent) -> None:
        """Link lost. Nothing is sent to the aircraft (it can't hear). Its intent stays reserved
        (position unknown, so the whole remaining route up to the nominal landing plus margin),
        a ledger line is written and a human card goes up. Reserving tightens, so it applies on
        that tick; releasing early is a human's call."""
        at = _position(self.telemetry.get(event.asset) or {})
        standing = self.intents.get(event.asset)
        standing = standing if standing is not None and standing.live else None
        reserved_until = None
        if standing is not None:
            reserved_until = standing.reserve_dark(event.last_seen_tick, at)
            self._dark[event.asset] = standing
        behaviour = self.performance.lost_link.behaviour
        detail = {"resource": event.asset, "since_tick": event.since_tick,
                  "last_seen_tick": event.last_seen_tick, "declared_tick": event.tick,
                  "intent": standing.id if standing is not None else None,
                  "behaviour": behaviour, "reserved_until_tick": reserved_until,
                  "last_position": _position_dict(at)}
        reason = (f"{event.asset} 텔레메트리가 틱 {event.since_tick} 부터 없음 — {behaviour}, "
                  + (f"승인 경로 + 착륙 기둥을 틱 {reserved_until} 까지 예약"
                     if reserved_until is not None else "예약할 의도 없음"))
        self._ledger_link("link_lost", event.asset, reason, detail, standing)
        self._raise_link_card(event.asset, detail, standing)

    def _link_restored(self, event: LinkEvent) -> None:
        """Telemetry is back. Check whether the aircraft stayed inside the cleared volume while
        the link was lost (conformance), undo the extended window and take the card down. It is
        visible again, so judgement goes by telemetry from here."""
        at = _position(self.telemetry.get(event.asset) or {})
        intent = self._dark.pop(event.asset, None)
        self._released.discard(event.asset)
        conforming = intent.covers(*at) if intent is not None and at is not None else None
        if intent is not None:
            intent.release_dark()
        detail = {"resource": event.asset, "since_tick": event.since_tick,
                  "last_seen_tick": event.last_seen_tick, "restored_tick": event.tick,
                  "dark_ticks": event.tick - event.since_tick,
                  "intent": intent.id if intent is not None else None,
                  "conforming": conforming, "position": _position_dict(at)}
        where = ("승인한 부피 안" if conforming
                 else "승인한 부피 밖" if conforming is False else "잡아 둔 의도 없음")
        reason = (f"{event.asset} 텔레메트리가 틱 {event.tick} 에 돌아옴 "
                  f"({detail['dark_ticks']}틱 끊김) — {where}")
        self._ledger_link("link_restored", event.asset, reason, detail, intent)
        if conforming is False:
            self._ledger_link_nonconformance(event, intent, at)
        self._drop_link_card(event.asset, detail)
        self._observe()     # advance the intent from fresh telemetry (arrived if it has landed)

    def _ledger_link(self, code: str, asset: str, reason: str, detail: dict,
                     intent: Intent | None) -> None:
        """One line for a lost or restored link (outcome noted). Nothing is executed: an
        aircraft with a lost link can't hear."""
        noted = Proposal(asset_id=asset, action=code, cost_usd=0.0, blast_radius="none",
                         author="runtime", rationale=reason[:180], params=dict(detail))
        decision = Decision(noted.id, Verdict.AUTO, reason, code=code, detail=dict(detail))
        entry = self.ledger.open_entry(
            noted, decision, self._context(None, ["link"], intent.id if intent else None))
        self.ledger.close_entry(entry, "noted")

    def _ledger_link_nonconformance(self, event: LinkEvent, intent: Intent, at) -> None:
        """It left the cleared volume while the link was lost. Not undone, only recorded."""
        noted = Proposal(asset_id=event.asset, action="conformance", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"링크가 끊긴 사이 승인한 부피 밖 (틱 {event.tick} 에 "
                                   "다시 보임)",
                         params={"intent": intent.id, "kind": "lost_link",
                                 "since_tick": event.since_tick, "restored_tick": event.tick,
                                 "position": _position_dict(at)})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{event.asset} 가 링크가 끊긴 사이 승인한 부피 밖에 있었습니다",
                            code="nonconforming",
                            detail={"resource": event.asset, "intent": intent.id,
                                    "kind": "lost_link", "restored_tick": event.tick,
                                    "position": _position_dict(at)})
        entry = self.ledger.open_entry(noted, decision,
                                       self._context(None, ["conformance"], intent.id))
        self.ledger.close_entry(entry, "noted")

    def _confirm_lost_link(self, proposal: Proposal, decision: Decision, actor: str,
                           allow: bool, card=None) -> Decision:
        """The human's answer. Approval releases the held space now: it means the human knows
        where the aircraft is, and that responsibility stays on the ledger under their name.
        Refusal keeps it until telemetry returns. Either way nothing is sent to the aircraft."""
        asset = proposal.asset_id
        self._link_cards.pop(asset, None)
        if allow and self.links.lost(asset):
            standing = self._dark.get(asset)
            if standing is not None and standing.live:
                self.intents.end(asset, "released")
            self._released.add(asset)
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 {asset} 의 잡아 둔 공간을 풀었습니다"
            decision.code = "lost_link_released"
            self._close_card(card, proposal, decision, "done", "link:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = (f"{actor} 가 텔레메트리가 돌아올 때까지 공간을 잡아 둡니다"
                           if self.links.lost(asset) else "링크가 이미 돌아왔습니다")
        decision.code = "lost_link_kept"
        self._close_card(card, proposal, decision, "denied", "link:human")
        return decision

    def _links_snapshot(self) -> dict:
        with self._link_lock:
            return self.links.snapshot()


def _position(state: dict) -> tuple[float, float, float] | None:
    """Position from telemetry (lat, lon, alt_m), or None if unknown."""
    if state.get("lat") is None or state.get("lon") is None:
        return None
    return float(state["lat"]), float(state["lon"]), float(state.get("alt_m") or 0.0)


def _position_dict(at: tuple[float, float, float] | None) -> dict | None:
    if at is None:
        return None
    return {"lat": round(at[0], 6), "lon": round(at[1], 6), "alt_m": round(at[2], 1)}
