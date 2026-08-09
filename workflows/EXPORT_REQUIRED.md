# ComfyUI API workflow export

The AGEWEC runtime needs ComfyUI's **API-format** workflow JSON. The normal
canvas/workflow JSON (`nodes` and `links`) cannot be submitted to `/prompt`.

1. Start ComfyUI Desktop in Local mode.
2. Open the working LTX image-to-video graph.
3. In ComfyUI settings, enable developer/dev mode.
4. Use **Save (API Format)** or **Export API**.
5. Save the result as:

   `workflows/ltx_i2v_api.json`

6. Keep ComfyUI Desktop running and verify without generating:

   ```bash
   uv run python -m agewec_v2.comfy_check
   ```

The checker validates the server, installed node types, workflow format and
automatic input mapping. It does not queue a generation.

The exported workflow has passed the checker, and `config_llm.yaml` now uses `backend: comfy`.

The initial safety setting is `max_video_cuts_per_run: 1`, so the first run
cannot accidentally queue every planned video cut.
