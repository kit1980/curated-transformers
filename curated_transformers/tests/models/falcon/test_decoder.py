import pytest
import torch

from curated_transformers.layers.attention import AttentionMask
from curated_transformers.models.falcon.decoder import FalconDecoder
from curated_transformers.tests.util import torch_assertclose

from ...compat import has_hf_transformers, transformers
from ...conftest import TORCH_DEVICES

VOCAB_SIZE = 1024

# We do not have tests to check caching/positions against upstream, there
# are two issues with the upstream model:
#
# 1. It uses the torch scaled dot-product attention, but that has a bug
#    in generating causal masks:
#
#    https://github.com/pytorch/pytorch/issues/103082
#
# 2. When using a cache, the upstream implementation does not take into
#    account that we need to index by position into the rotary embeddings.
#
# We test caching instead by comparing output of the model with caching
# against output without caching.


FALCON_TEST_MODELS = [
    (
        "explosion-testing/falcon-no-parallel-attn-test",
        "f9122de6cafad10159af214786fa11b89cc37a89",
    ),
    ("explosion-testing/falcon-test", "24ff3d5fd83b4d174888356f20e61349f6cbf467"),
    (
        "explosion-testing/refined-web-model-test",
        "57a7a9829a4b6fce833152c4c20a46c7056f9cc1",
    ),
]


@pytest.mark.skipif(not has_hf_transformers, reason="requires huggingface transformers")
@pytest.mark.parametrize("torch_device", TORCH_DEVICES)
@pytest.mark.parametrize("model_revision", FALCON_TEST_MODELS)
def test_decoder(torch_device, model_revision):
    model, revision = model_revision

    hf_model = transformers.AutoModel.from_pretrained(
        model,
        # Safe because it is under our control.
        trust_remote_code=True,
        # Avoid warnings about trusting remote code without a revision.
        revision=revision,
    )
    hf_model.to(torch_device)
    hf_model.eval()

    model = FalconDecoder.from_hf_hub(
        name=model, revision=revision, device=torch_device
    )
    model.eval()

    torch.manual_seed(0)
    X = torch.randint(0, hf_model.config.vocab_size, (2, 10), device=torch_device)

    with torch.no_grad():
        Y = model(X).last_hidden_layer_state
        Y_hf = hf_model(X).last_hidden_state

    torch_assertclose(Y, Y_hf)


@pytest.mark.skipif(not has_hf_transformers, reason="requires huggingface transformers")
@pytest.mark.parametrize("torch_device", TORCH_DEVICES)
@pytest.mark.parametrize("model_revision", FALCON_TEST_MODELS)
def test_decoder_with_cache(torch_device, model_revision):
    model, revision = model_revision

    model = FalconDecoder.from_hf_hub(
        name=model, revision=revision, device=torch_device
    )
    model.eval()

    torch.manual_seed(0)
    X = torch.randint(0, VOCAB_SIZE, (2, 10), device=torch_device)
    X_rest = torch.randint(0, VOCAB_SIZE, (2, 10), device=torch_device)

    with torch.no_grad():
        Y = model(X, store_cache=True)
        Y = model(X_rest, cache=Y.cache).last_hidden_layer_state
        Y_no_cache = model(torch.cat([X, X_rest], dim=1)).last_hidden_layer_state

    torch_assertclose(Y, Y_no_cache[:, 10:, :])
