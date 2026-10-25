"""XENIO UI V1: a local web interface over XENIO's real components (``python -m app.main --ui``).

The browser is a presentation layer. Every decision stays here, in XENIO's process: chat goes through the same
Orchestrator as text and voice mode, approvals through the same PermissionGate, the engineering team is the V6
service, schedules are Hermes' own cron jobs, memory is Hermes' memory files. See docs/UI.md.
"""
