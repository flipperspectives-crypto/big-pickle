from .config import settings

PRICING = {
    # per 1M tokens, (input, output), USD
    "groq": {
        "llama-3.3-70b-versatile": (0.59, 0.79),
        "llama-3.1-8b-instant": (0.05, 0.08),
        "qwen-2.5-32b": (0.79, 0.79),
        "openai/gpt-oss-120b": (0.25, 0.25),
        "openai/gpt-oss-20b": (0.10, 0.10),
    },
    "cerebras": {
        "gpt-oss-120b": (0.25, 0.25),
        "gemma-4-31b": (0.50, 1.50),
        "zai-glm-4.7": (0.50, 1.50),
    },
    "deepinfra": {
        "meta-llama/llama-3.3-70b-instruct": (0.20, 0.20),
        "meta-llama/llama-3.1-8b-instruct": (0.06, 0.06),
    },
    "together": {
        "meta-llama/llama-3.3-70b-instruct": (0.88, 0.88),
        "meta-llama/llama-3.1-8b-instruct": (0.18, 0.18),
    },
    "fireworks": {
        "accounts/fireworks/models/llama-v3p3-70b-instruct": (0.90, 0.90),
        "accounts/fireworks/models/llama-v3p1-8b-instruct": (0.20, 0.20),
    },
    "huggingface": {
        "meta-llama/llama-3.3-70b-instruct": (0.20, 0.20),
        "meta-llama/llama-3.1-8b-instruct": (0.05, 0.05),
    },
    "openai": {
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
    },
    "anthropic": {
        "claude-3-5-haiku": (0.80, 4.00),
        "claude-3-5-sonnet": (3.00, 15.00),
    },
    "openrouter": {
        "*": (0.15, 0.60),  # fallback guess for unlisted
    },
    "local": {
        "*": (0.0, 0.0),  # free local inference (Ollama)
    },
}

# canonical model name -> ordered provider list to try (failover)
ROUTES = {
    "llama-3.3-70b": ["groq", "cerebras", "deepinfra", "huggingface"],
    "llama-3.1-8b": ["groq", "cerebras", "deepinfra", "huggingface"],
    "llama-3.3-70b-versatile": ["groq"],
    "llama-3.3-70b-instruct": ["deepinfra", "together", "huggingface"],
    "llama-3.1-8b-instruct": ["deepinfra", "together", "huggingface"],
    "qwen-2.5-32b": ["groq"],
    "gpt-oss-120b": ["groq", "cerebras"],
    "gpt-oss-20b": ["groq", "cerebras"],
    "gemma-4-31b": ["cerebras"],
    "zai-glm-4.7": ["cerebras"],
    "gpt-4o-mini": ["openai"],
    "gpt-4o": ["openai"],
    "claude-3-5-haiku": ["anthropic"],
    "claude-3-5-sonnet": ["anthropic"],
    "qwen2.5-0.5b": ["local"],
    "qwen2.5-3b": ["local"],
    "lucy": ["local"],
    "lucy-light": ["local"],
    "llama-3.2": ["local"],
}

# provider -> actual model id to send upstream (override mapping)
PROVIDER_MODEL = {
    "groq": {"gpt-oss-120b": "openai/gpt-oss-120b", "gpt-oss-20b": "openai/gpt-oss-20b"},
    "cerebras": {"llama-3.3-70b": "llama-3.3-70b", "llama-3.1-8b": "llama-3.1-8b"},
    "fireworks": {
        "llama-3.3-70b": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "llama-3.1-8b": "accounts/fireworks/models/llama-v3p1-8b-instruct",
    },
    "huggingface": {
        "llama-3.1-8b-instruct": "meta-llama/Llama-3.1-8B-Instruct",
        "llama-3.3-70b-instruct": "meta-llama/Llama-3.3-70B-Instruct",
        "llama-3.3-70b": "meta-llama/Llama-3.3-70B-Instruct",
        "llama-3.1-8b": "meta-llama/Llama-3.1-8B-Instruct",
    },
}

OPENAI_COMPATIBLE = {
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "local": "http://127.0.0.1:11434/v1",
}

ANTHROPIC = {"anthropic": "https://api.anthropic.com/v1"}


def providers_for(model: str) -> list[str]:
    if ":" in model:
        name, _, rest = model.partition(":")
        if name in OPENAI_COMPATIBLE or name in ANTHROPIC:
            return [name] if rest else [name]
    return ROUTES.get(model, ["openrouter"])


def upstream_model(provider: str, model: str) -> str:
    mapped = PROVIDER_MODEL.get(provider, {})
    base = model.split(":", 1)[-1]
    for canon, alias in mapped.items():
        if base == canon or base.endswith("/" + canon):
            return alias
    return base


def price_for(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    table = PRICING.get(provider, {})
    up = upstream_model(provider, model)
    if up not in table:
        up = "*" if "*" in table else "meta-llama/llama-3.3-70b-instruct"
    pin, pout = table.get(up, (0.0, 0.0))
    return (prompt_tokens / 1_000_000) * pin + (completion_tokens / 1_000_000) * pout


def is_free_model(model: str) -> bool:
    return providers_for(model) == ["local"]


def is_openai_compatible(provider: str) -> bool:
    return provider in OPENAI_COMPATIBLE


def base_url(provider: str) -> str:
    return OPENAI_COMPATIBLE.get(provider, ANTHROPIC.get(provider, ""))


def needs_key(provider: str) -> bool:
    if provider == "huggingface":
        return bool(settings.HF_KEY)
    return True
