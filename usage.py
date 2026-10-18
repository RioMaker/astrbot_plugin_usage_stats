"""Read reported token usage without estimating text or double-counting cache."""

from collections.abc import Mapping


def field(obj, key, default=None):
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def number(obj, key):
    value = field(obj, key)
    # bool, negative, float and malformed upstream data are not valid counters.
    return value if type(value) is int and 0 <= value < 2**63 else None


def normalize_usage(response):
    """Return (input, output, total); None means the provider did not report it."""
    usage = field(response, "usage")
    raw = field(response, "raw_completion")
    # AstrBot's OpenAI adapter substitutes TokenUsage() when the server omitted
    # usage. Do not turn that sentinel into an apparently measured zero.
    if (
        raw is not None
        and field(raw, "usage", "absent") is None
        and all(
            number(usage, key) == 0 for key in ("input_other", "input_cached", "output")
        )
    ):
        return None, None, None
    if usage is not None:
        other = number(usage, "input_other")
        cached = number(usage, "input_cached")
        output = number(usage, "output")
        if other is not None and cached is not None:
            input_ = other + cached
            return input_, output, input_ + output if output is not None else None
        standard = _standard_usage(usage)
        if any(value is not None for value in standard):
            return standard

    usage = field(raw, "usage")
    if usage is not None:
        # Anthropic input_tokens excludes cache reads/writes; OpenAI prompt_tokens
        # and Responses input_tokens already include cached tokens.
        if (
            field(usage, "cache_read_input_tokens") is not None
            or field(usage, "cache_creation_input_tokens") is not None
        ):
            input_ = number(usage, "input_tokens")
            read = number(usage, "cache_read_input_tokens")
            created = number(usage, "cache_creation_input_tokens")
            if input_ is not None:
                input_ += (read or 0) + (created or 0)
            output = number(usage, "output_tokens")
            return input_, output, _total(usage, input_, output)
        return _standard_usage(usage)

    # Google SDK uses snake_case, its wire JSON uses camelCase.
    usage = field(raw, "usage_metadata", field(raw, "usageMetadata"))
    if usage is not None:

        def google(snake, camel):
            value = number(usage, snake)
            return value if value is not None else number(usage, camel)

        input_ = google("prompt_token_count", "promptTokenCount")
        output = google("candidates_token_count", "candidatesTokenCount")
        thoughts = google("thoughts_token_count", "thoughtsTokenCount")
        if output is not None:
            output += thoughts or 0
        total = google("total_token_count", "totalTokenCount")
        return input_, output, total
    return None, None, None


def _total(usage, input_, output):
    total = number(usage, "total_tokens")
    if total is not None:
        return total
    return input_ + output if input_ is not None and output is not None else None


def _standard_usage(usage):
    input_ = number(usage, "prompt_tokens")
    if input_ is None:
        input_ = number(usage, "input_tokens")
    output = number(usage, "completion_tokens")
    if output is None:
        output = number(usage, "output_tokens")
    return input_, output, _total(usage, input_, output)


def provider_labels(provider, response):
    """Metadata only: never serialize provider configs, requests or completions."""
    try:
        meta = provider.meta()
    except Exception:
        meta = None
    provider_id = str(field(meta, "id") or type(provider).__name__)
    model = field(field(response, "raw_completion"), "model")
    model = model or field(meta, "model") or "未返回模型名"
    return provider_id, str(model)
