# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
from copy import deepcopy
from types import SimpleNamespace

import pytest

from vllm_ascend.models.glm5next.recoverssm_config import (
    get_glm_recoverssm_pool_model_name,
    validate_glm_recoverssm,
)


def make_config(extra=None, multi=False):
    store = {
        "kv_connector": "AscendStoreConnector",
        "kv_connector_extra_config": {"backend": "memcache", **(extra or {})},
    }
    transfer = (
        {
            "kv_connector": "MultiConnector",
            "kv_connector_extra_config": {"connectors": [{"kv_connector": "MooncakeConnectorV2"}, store]},
        }
        if multi
        else store
    )
    return SimpleNamespace(
        model_config=SimpleNamespace(hf_text_config=SimpleNamespace(model_type="glm5_next_text"), dtype="bfloat16"),
        speculative_config=SimpleNamespace(num_speculative_tokens=5),
        cache_config=SimpleNamespace(
            use_replayssm=True,
            enable_prefix_caching=True,
            mamba_cache_mode="align",
            mamba_cache_dtype="auto",
            mamba_ssm_cache_dtype="float32",
            block_size=128,
            mamba_block_size=128,
        ),
        mamba_config=SimpleNamespace(enable_stochastic_rounding=False),
        parallel_config=SimpleNamespace(
            tensor_parallel_size=8,
            pipeline_parallel_size=1,
            prefill_context_parallel_size=1,
            decode_context_parallel_size=1,
        ),
        kv_transfer_config=SimpleNamespace(**transfer),
    )


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("layerwise", [False, True])
def test_prefill_memcache_accepts_bulk_and_independent_layerwise(multi, layerwise):
    config = make_config({"use_layerwise": layerwise, "save_decode_cache": False}, multi)
    validate_glm_recoverssm(config)


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize(
    "extra,message",
    [
        ({"backend": "mooncake"}, "memcache backend"),
        ({"backend": "yuanrong"}, "memcache backend"),
        ({"save_decode_cache": True}, "Prefill prefix caching only"),
        ({"discard_partial_chunks": False}, "discard_partial_chunks"),
        ({"load_async": True}, "synchronous bulk loads"),
        ({"use_layerwise": True, "layerwise_num_shared_buffers": 2}, "layerwise_num_shared_buffers"),
    ],
)
def test_prefill_memcache_rejects_unsupported_modes(extra, message, multi):
    with pytest.raises(ValueError, match=message):
        validate_glm_recoverssm(make_config(extra, multi))


def test_prefill_memcache_requires_align_prefix_checkpoints():
    config = make_config()
    config.cache_config.enable_prefix_caching = False
    config.cache_config.mamba_cache_mode = "none"
    with pytest.raises(ValueError, match="requires prefix caching"):
        validate_glm_recoverssm(config)


@pytest.mark.parametrize("connector", ["UnsupportedConnector", "MooncakeLayerwiseConnector"])
def test_multi_connector_validates_every_child(connector):
    config = make_config(multi=True)
    config.kv_transfer_config.kv_connector_extra_config["connectors"].append({"kv_connector": connector})
    with pytest.raises(ValueError, match="KV transfer requires"):
        validate_glm_recoverssm(config)


def test_empty_multi_connector_is_rejected():
    config = make_config(multi=True)
    config.kv_transfer_config.kv_connector_extra_config["connectors"] = []
    with pytest.raises(ValueError, match="requires child connectors"):
        validate_glm_recoverssm(config)


def test_pool_namespace_is_shared_by_identical_prefill_and_decode_layouts():
    prefill = make_config({"use_layerwise": True})
    decode = make_config({"use_layerwise": False}, multi=True)
    assert get_glm_recoverssm_pool_model_name(prefill, "glm") == get_glm_recoverssm_pool_model_name(decode, "glm")
    assert get_glm_recoverssm_pool_model_name(prefill, "glm") != "glm"


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("speculative_config", "num_speculative_tokens", 3),
        ("parallel_config", "tensor_parallel_size", 4),
        ("model_config", "dtype", "float32"),
        ("cache_config", "mamba_cache_dtype", "float32"),
        ("cache_config", "mamba_ssm_cache_dtype", "bfloat16"),
        ("cache_config", "block_size", 64),
        ("cache_config", "mamba_block_size", 256),
    ],
)
def test_pool_namespace_separates_incompatible_layouts(section, field, value):
    config = make_config()
    other = deepcopy(config)
    setattr(getattr(other, section), field, value)
    assert get_glm_recoverssm_pool_model_name(config, "glm") != get_glm_recoverssm_pool_model_name(other, "glm")


def test_disabled_recoverssm_keeps_native_pool_keys():
    config = make_config()
    config.cache_config.use_replayssm = False
    assert get_glm_recoverssm_pool_model_name(config, "glm") == "glm"


def test_other_models_keep_native_pool_keys():
    config = make_config()
    config.model_config.hf_text_config.model_type = "kimi_k3"
    assert get_glm_recoverssm_pool_model_name(config, "kimi") == "kimi"
