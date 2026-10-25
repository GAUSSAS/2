"""可选的 LLM 接入层。

AgentFlow 的默认链路**完全离线、确定性**（离线模板 + AST 规则），
这样作业/评审环境不需要任何 API Key 就能复现整个流程。

同时留好接入真实 LLM 的插槽：设置下列环境变量后，``get_provider()``
会返回一个 OpenAI 兼容的 Provider（DeepSeek / OpenAI / vLLM / Ollama 均可）：:

    AGENTFLOW_LLM_BASE_URL=https://api.deepseek.com/v1
    AGENTFLOW_LLM_API_KEY=sk-...
    AGENTFLOW_LLM_MODEL=deepseek-chat

接入方式：把某个 Agent 的 ``mode`` 参数设为 ``"llm"``（详见 ``code_agents.CoderAgent``），
或在自定义 Agent 里调用 ``get_provider().complete(...)``。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class LLMError(RuntimeError):
    """LLM 调用失败。"""


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 30.0
    temperature: float = 0.2
    max_tokens: int = 2048

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        base_url = os.environ.get("AGENTFLOW_LLM_BASE_URL", "").rstrip("/")
        api_key = os.environ.get("AGENTFLOW_LLM_API_KEY", "")
        model = os.environ.get("AGENTFLOW_LLM_MODEL", "")
        if not (base_url and api_key and model):
            return None
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_s=float(os.environ.get("AGENTFLOW_LLM_TIMEOUT", "30")),
            temperature=float(os.environ.get("AGENTFLOW_LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.environ.get("AGENTFLOW_LLM_MAX_TOKENS", "2048")),
        )


class LLMProvider:
    """Provider 接口：只有一个方法，方便替换与 mock。"""

    name = "base"

    def complete(self, prompt: str, *, system: str | None = None,
                 metadata: Mapping[str, Any] | None = None) -> str:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}


class OpenAICompatibleProvider(LLMProvider):
    """调用 ``POST {base_url}/chat/completions`` 的最小实现（仅用标准库）。"""

    name = "openai-compatible"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 metadata: Mapping[str, Any] | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 返回结构异常: {payload}") from exc
        self.calls.append({"prompt_chars": len(prompt), "reply_chars": len(text), "meta": dict(metadata or {})})
        return text

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "calls": len(self.calls),
        }


def get_provider(config: LLMConfig | None = None) -> LLMProvider | None:
    """按环境变量构造 Provider；未配置则返回 ``None``（调用方走离线模板）。"""
    if config is None:
        config = LLMConfig.from_env()
    if config is None:
        return None
    return OpenAICompatibleProvider(config)
