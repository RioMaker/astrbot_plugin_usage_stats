from types import SimpleNamespace as NS

from usage import normalize_usage, provider_labels


def test_astrbot_cached_input_is_included_exactly_once():
    assert normalize_usage(NS(usage=NS(input_other=80, input_cached=20, output=7))) == (
        100,
        7,
        107,
    )


def test_openai_cache_and_reasoning_details_not_added_again():
    assert normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 30,
                    "total_tokens": 130,
                    "prompt_tokens_details": {"cached_tokens": 20},
                    "completion_tokens_details": {"reasoning_tokens": 10},
                }
            }
        }
    ) == (100, 30, 130)


def test_missing_is_not_zero():
    assert normalize_usage(NS(usage=None)) == (None, None, None)
    assert normalize_usage({"usage": {"input_tokens": 10}}) == (10, None, None)
    assert normalize_usage({"usage": {"input_tokens": 0, "output_tokens": 0}}) == (
        0,
        0,
        0,
    )


def test_astrbot_openai_missing_usage_zero_sentinel_is_unknown():
    zero = NS(input_other=0, input_cached=0, output=0)
    assert normalize_usage(NS(usage=zero, raw_completion=NS(usage=None))) == (
        None,
        None,
        None,
    )
    assert normalize_usage(
        NS(
            usage=zero,
            raw_completion=NS(
                usage=NS(prompt_tokens=0, completion_tokens=0, total_tokens=0)
            ),
        )
    ) == (0, 0, 0)


def test_raw_anthropic_and_google():
    assert normalize_usage(
        {
            "raw_completion": {
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 3,
                }
            }
        }
    ) == (103, 7, 110)
    assert normalize_usage(
        {
            "raw_completion": {
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 10,
                    "totalTokenCount": 130,
                }
            }
        }
    ) == (100, 30, 130)


def test_invalid_tokens_not_silently_coerced():
    assert normalize_usage(
        {"usage": {"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "8"}}
    ) == (None, None, None)


def test_model_label_uses_returned_model_and_never_config():
    provider = NS(meta=lambda: NS(id="provider-a", model="default"), api_key="secret")
    assert provider_labels(provider, {"raw_completion": {"model": "actual"}}) == (
        "provider-a",
        "actual",
    )
