"""Proxy workers that run a stage on a remote ComfyUI server instead of in this container.

Same request.json in and same output.json out as image_worker / pixal3d_worker, so the orchestrator
does not know or care which backend produced the artifacts. Module-level imports stay stdlib-only:
these containers have no torch and hold no GPU memory.
"""
