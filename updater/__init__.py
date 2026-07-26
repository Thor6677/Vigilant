"""Vigilant's update sidecar.

Lives outside `app/` because it ships in a DIFFERENT image: the app must never
gain the Docker socket, and the updater must never gain the app's dependencies.
The one thing they share is `app.ops.version`, which updater/Dockerfile copies
in — a version comparison implemented twice would eventually disagree with
itself about which release is newer.
"""
