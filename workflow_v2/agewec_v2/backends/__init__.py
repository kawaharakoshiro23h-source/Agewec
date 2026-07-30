"""Media backends used only by workflow_v2."""

from .comfy import ComfyClient, ComfyGenerationRequest

__all__ = ["ComfyClient", "ComfyGenerationRequest"]
