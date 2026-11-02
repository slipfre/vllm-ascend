# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""GLM KDA RecoverSSM uses the upstream ReplaySSM configuration switch."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vllm.config import VllmConfig


def is_glm_recoverssm_model(vllm_config: VllmConfig) -> bool:
    model_config = vllm_config.model_config
    return model_config is not None and model_config.hf_text_config.model_type in ("glm5_next", "glm5_next_text")


def use_glm_recoverssm(vllm_config: VllmConfig) -> bool:
    return is_glm_recoverssm_model(vllm_config) and vllm_config.cache_config.use_replayssm


def get_glm_recoverssm_pool_model_name(vllm_config: VllmConfig, model_name: str) -> str:
    """Keep incompatible native and RecoverSSM pool payloads in separate keys."""
    cache = vllm_config.cache_config
    if getattr(cache, "use_replayssm", False) is not True or not is_glm_recoverssm_model(vllm_config):
        return model_name
    layout = (
        vllm_config.speculative_config.num_speculative_tokens,
        vllm_config.parallel_config.tensor_parallel_size,
        str(vllm_config.model_config.dtype),
        str(cache.mamba_cache_dtype),
        str(cache.mamba_ssm_cache_dtype),
        cache.block_size,
        cache.mamba_block_size,
    )
    layout_hash = hashlib.sha256(json.dumps(layout).encode()).hexdigest()
    return f"{model_name}@glm-recoverssm-v1-{layout_hash}"


def _validate_glm_recoverssm_connector(vllm_config: VllmConfig, connector: str, extra_config: dict) -> None:
    if connector in ("MooncakeConnectorV2", "MooncakePullConnector"):
        return
    if connector == "MultiConnector":
        connectors = extra_config.get("connectors", [])
        if not connectors:
            raise ValueError("GLM RecoverSSM MultiConnector requires child connectors")
        for child in connectors:
            _validate_glm_recoverssm_connector(
                vllm_config, child["kv_connector"], child.get("kv_connector_extra_config") or {}
            )
        return
    if connector != "AscendStoreConnector":
        raise ValueError(
            "GLM RecoverSSM KV transfer requires MooncakeConnectorV2, MooncakePullConnector or AscendStoreConnector"
        )
    if str(extra_config.get("backend", "mooncake")).lower() != "memcache":
        raise ValueError("GLM RecoverSSM AscendStoreConnector requires the memcache backend")
    if not vllm_config.cache_config.enable_prefix_caching:
        raise ValueError("GLM RecoverSSM Memcache requires prefix caching with Mamba cache mode align")
    if extra_config.get("save_decode_cache", False):
        raise ValueError("GLM RecoverSSM Memcache supports Prefill prefix caching only; disable save_decode_cache")
    if not extra_config.get("discard_partial_chunks", True):
        raise ValueError("GLM RecoverSSM Memcache requires discard_partial_chunks=True")
    if extra_config.get("use_layerwise", False):
        # Omit the buffer count to retain independent storage for every layer.
        # Shared buffers must offload Decode state before RecoverSSM commits it.
        if extra_config.get("layerwise_num_shared_buffers") is not None:
            raise ValueError("GLM RecoverSSM Memcache does not support layerwise_num_shared_buffers")
    elif extra_config.get("load_async", False):
        raise ValueError("GLM RecoverSSM Memcache requires synchronous bulk loads; disable load_async")


def validate_glm_recoverssm(vllm_config: VllmConfig) -> None:
    if vllm_config.speculative_config is None:
        raise ValueError("GLM RecoverSSM requires speculative decoding")
    cache = vllm_config.cache_config
    # TODO: FP16 KV cache with RecoverSSM has not been validated.
    if vllm_config.mamba_config.enable_stochastic_rounding:
        raise ValueError("GLM RecoverSSM does not support stochastic rounding")
    expected_cache_mode = "align" if cache.enable_prefix_caching else "none"
    if cache.mamba_cache_mode != expected_cache_mode:
        raise ValueError(
            f"GLM RecoverSSM requires Mamba cache mode {expected_cache_mode} with this prefix caching setting"
        )
    parallel = vllm_config.parallel_config
    if parallel.pipeline_parallel_size != 1 or parallel.prefill_context_parallel_size != 1:
        raise ValueError("GLM RecoverSSM currently requires PP=1 and PCP=1")
    if parallel.decode_context_parallel_size != 1:
        raise ValueError("GLM RecoverSSM currently requires DCP=1")
    transfer = vllm_config.kv_transfer_config
    if transfer is not None:
        _validate_glm_recoverssm_connector(
            vllm_config, transfer.kv_connector, getattr(transfer, "kv_connector_extra_config", None) or {}
        )
