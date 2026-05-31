"""GIS pipeline for the walking-around project.

Stages run as modules from the repo root, e.g. ``uv run python -m pipeline.config``.
Shared spatial parameters (center, bbox, grid, CRS, output paths) live in
``pipeline.config`` — import them, never hardcode.
"""
