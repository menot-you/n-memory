"""Prose projection of world events. Goldens never parse this text."""

from __future__ import annotations

from nmemory_evals.world import Event, EventKind


def render(event: Event) -> str:
    """Render a simulated event into canonical engineering log prose."""
    match event.kind:
        case EventKind.DEPLOY:
            return (
                f"Deploy {event.id}: engineer {event.engineer} deployed "
                f"service {event.service} version {event.version}."
            )
        case EventKind.INCIDENT_OPEN:
            incident_id = event.incident or event.id
            caused_by = event.caused_by or "unknown"
            return (
                f"Incident {incident_id}: service {event.service} version {event.version} "
                f"was caused by deploy {caused_by}; opened by engineer {event.engineer}."
            )
        case EventKind.OWNERSHIP_TRANSFER:
            return (
                f"Ownership transfer {event.id}: service {event.service} owner "
                f"transferred to engineer {event.engineer} at version {event.version}."
            )
        case EventKind.RESOLVE:
            incident_id = event.incident or "unknown"
            return (
                f"Resolution {event.id}: engineer {event.engineer} resolved "
                f"incident {incident_id} for service {event.service} version {event.version}."
            )
