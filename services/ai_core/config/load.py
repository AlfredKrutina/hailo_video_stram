from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from shared.schemas.config import AppConfig


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_app_config(config_path: str | None = None) -> AppConfig:
    path = Path(
        config_path
        or os.environ.get("RPY_CONFIG", str(Path(__file__).resolve().parents[3] / "config" / "default.yaml")),
    )
    data: dict = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    env_overrides: dict = {}
    if url := os.environ.get("REDIS_URL"):
        env_overrides["redis_url"] = url
    if uri := os.environ.get("SOURCE_URI"):
        env_overrides.setdefault("source", {})["uri"] = uri
    if os.environ.get("USE_HAILO", "").lower() in ("0", "false", "no"):
        env_overrides["use_hailo"] = False
    # RPY_INFER_BACKEND, RPY_ONNX_MODEL_PATH, RPY_HAILO_HEF_PATH — čte inference.factory / hailo_real
    data = _deep_merge(data, env_overrides)
    try:
        return AppConfig.model_validate(data)
    except ValidationError:
        raise
