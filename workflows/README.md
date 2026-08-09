# ComfyUI workflow

ComfyUIで手動動作を確認したLTX Image-to-Videoワークフローを、
API形式で次の名前に保存します。

```text
ltx_i2v_api.json
```

保存後、JSON内の各ノードIDを確認して `../config.yaml` の
`comfy.inputs` を設定してください。

最低限必要なマッピング:

- `image`
- `positive_prompt`

推奨マッピング:

- `negative_prompt`
- `width`
- `height`
- `frames`
- `steps`
- `fps`
- `seed`
- `file_prefix`

設定が終わるまでは `production.backend: mock` のまま使用します。
