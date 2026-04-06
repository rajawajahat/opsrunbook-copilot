"""Shared LLM client for OpsRunbook Copilot Lambda functions."""
from __future__ import annotations

import json
import os
from typing import Optional

import boto3

_ssm_client = None
_api_key_cache: dict[str, Optional[str]] = {}


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _read_ssm(path: str) -> Optional[str]:
    """Read a parameter from SSM, caching the result for Lambda warm starts.

    SSM values are stored JSON-encoded (e.g. '"actual_key"') so we
    use json.loads to unwrap them.
    """
    if path in _api_key_cache:
        return _api_key_cache[path]
    try:
        resp = _get_ssm_client().get_parameter(Name=path, WithDecryption=True)
        raw = resp["Parameter"]["Value"]
        if raw in ("REPLACE_ME", ""):
            _api_key_cache[path] = None
            return None
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            val = raw
        _api_key_cache[path] = val
        return val
    except Exception:
        _api_key_cache[path] = None
        return None


def _read_api_key(env_var: str, default_path: str) -> Optional[str]:
    ssm_path = os.environ.get(env_var, default_path)
    return _read_ssm(ssm_path)


def get_llm(provider: str = "stub", model: str | None = None):
    """
    Factory: returns a LangChain ChatModel or None when provider is "stub"
    or when the API key is not configured.

    Supported providers: "groq", "gemini", "stub".
    """
    if provider == "stub":
        return None

    if provider == "groq":
        api_key = _read_api_key("SSM_GROQ_API_KEY", "/opsrunbook/dev/groq/api_key")
        if not api_key:
            print("[WARN] Groq API key not configured; falling back to stub")
            return None
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=model or "llama-3.3-70b-versatile",
            api_key=api_key,
            temperature=0.2,
            max_retries=2,
        )

    if provider == "gemini":
        api_key = _read_api_key("SSM_GOOGLE_API_KEY", "/opsrunbook/dev/google/api_key")
        if not api_key:
            print("[WARN] Google API key not configured; falling back to stub")
            return None
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model or "gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0.2,
        )

    print(f"[WARN] Unknown LLM provider '{provider}'; falling back to stub")
    return None
