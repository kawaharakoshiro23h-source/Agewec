"""[LEGACY v1] LM Studio / OpenAI互換APIクライアント。

LM Studio 側で「サーバを起動」しておくこと（既定 http://localhost:1234/v1）。
接続先・モデル名は .env で指定する:
    LOCAL_BASE_URL=http://127.0.0.1:1234/v1
    LOCAL_API_KEY=lm-studio         # LM Studio はダミーで可（本物の鍵は不要）
    LLM_MODEL=<LM Studioで読み込み中のモデルID>

単体テスト（疎通確認）:
    uv run python -m agewec.llm
"""
from __future__ import annotations

import os

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _base() -> str:
    # LOCAL_* を優先。旧 OPENAI_* も一応拾う
    return (os.environ.get("LOCAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "http://127.0.0.1:1234/v1")


def _key() -> str:
    return (os.environ.get("LOCAL_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "lm-studio")


def chat(prompt: str, system: str | None = None,
         temperature: float = 0.7, timeout: float = 120.0) -> str:
    """1回の chat completion を投げて本文テキストを返す。"""
    base = _base()
    key = _key()
    model = os.environ.get("LLM_MODEL", "local-model")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def is_available() -> bool:
    """LLMサーバに繋がるかを軽く確認する。"""
    try:
        httpx.get(f"{_base()}/models", timeout=3.0)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("接続先:", _base())
    print("モデル:", os.environ.get("LLM_MODEL", "local-model"))
    print("疎通:", "OK" if is_available() else "NG（LM Studioのサーバは起動してる？）")
    print("---")
    print(chat("『北九州』を主題に、20文字以内のキャッチコピーを1つだけ。"))
