import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _norm_ollama(v: str) -> str:
    """Normalize an Ollama base URL: trim whitespace and any trailing slash.

    Default stays at the ordinary local development endpoint so tests and
    local dev keep working without configuration. The real (private) Windows
    Ollama endpoint is supplied via the OLLAMA_BASE_URL environment variable in
    production -- it is NEVER hard-coded in source.
    """
    v = (v or "").strip()
    if not v:
        v = "http://127.0.0.1:11434"
    return v.rstrip("/")


class Settings:
    # Admin API is fail-closed: there is NO usable default. If this is empty
    # (or the legacy insecure literal "admin-change-me") the admin API is
    # disabled and every admin endpoint rejects access. A strong key must be
    # supplied via the GATEWAY_ADMIN_KEY environment variable.
    ADMIN_KEY: str = _env("GATEWAY_ADMIN_KEY", "")
    # Public self-service signup is FAIL-CLOSED by default. It must be enabled
    # explicitly via GATEWAY_PUBLIC_SIGNUP_ENABLED=1|true|yes before the public
    # Funnel is reopened. There is no permissive default.
    PUBLIC_SIGNUP_ENABLED: bool = _env("GATEWAY_PUBLIC_SIGNUP_ENABLED", "false").lower() in (
        "1", "true", "yes"
    )
    DB_PATH: str = _env("GATEWAY_DB", "/data/gateway.db")
    STRIPE_API_KEY: str = _env("STRIPE_API_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = _env("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_SECRET: str = _env("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_ID: str = _env("STRIPE_PRICE_ID", "")
    STRIPE_PRICE_USD: float = float(_env("STRIPE_PRICE_USD", "10"))
    HF_KEY: str = _env("HF_TOKEN", "") or _env("GATEWAY_HF_KEY", "")
    MARKUP: float = float(_env("GATEWAY_MARKUP", "1.25"))
    # x402 machine-payable top-up (x402 v2, EIP-3009 USDC, Base Sepolia eip155:84532)
    X402_ENABLED: bool = _env("X402_ENABLED", "false").lower() in ("1", "true", "yes")
    X402_PAYTO: str = _env("X402_PAYTO", "")  # gateway receiving wallet (public address)
    X402_PRICE_USD: str = _env("X402_PRICE_USD", "0.001")
    X402_CHAIN_ID: str = _env("X402_CHAIN_ID", "eip155:84532")
    X402_FACILITATOR_URL: str = _env("X402_FACILITATOR_URL", "https://x402.org/facilitator")
    X402_RPC_URL: str = _env("X402_RPC_URL", "https://sepolia.base.org")
    # Local Ollama provider. Defaults to ordinary local dev; production points
    # this at the privately connected Windows Ollama via environment config.
    OLLAMA_BASE_URL: str = _norm_ollama(_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))

    # --- Perimeter guards for POST /v1/chat/completions ---------------------
    # These protect a single-process Windows laptop from abusive or accidental
    # public inference load. All defaults are conservative (fail toward safety)
    # and every value is overridable via environment configuration.
    #
    # Max raw request body size for chat completions (bytes). 256 KiB default.
    # Oversized bodies are rejected with 413 before any parsing/inference.
    MAX_CHAT_REQUEST_BYTES: int = int(_env("GATEWAY_MAX_CHAT_REQUEST_BYTES", "262144"))
    # Per authenticated key-id request rate limit (sliding window).
    CHAT_RATE_LIMIT_REQUESTS: int = int(_env("GATEWAY_CHAT_RATE_LIMIT_REQUESTS", "10"))
    CHAT_RATE_LIMIT_WINDOW_SECONDS: float = float(_env("GATEWAY_CHAT_RATE_LIMIT_WINDOW_SECONDS", "60"))
    # Max concurrent local (Ollama) generations. A single GPU/CPU laptop cannot
    # safely run more than one; default 1. Set >1 only on capable hardware.
    LOCAL_MAX_CONCURRENCY: int = int(_env("GATEWAY_LOCAL_MAX_CONCURRENCY", "1"))

    def provider_key(self, name: str) -> str:
        key = _env(f"GATEWAY_{name.upper()}_KEY")
        if name == "huggingface" and not key:
            key = self.HF_KEY
        return key


settings = Settings()
