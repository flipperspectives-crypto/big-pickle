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
    ADMIN_KEY: str = _env("GATEWAY_ADMIN_KEY", "admin-change-me")
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

    def provider_key(self, name: str) -> str:
        key = _env(f"GATEWAY_{name.upper()}_KEY")
        if name == "huggingface" and not key:
            key = self.HF_KEY
        return key


settings = Settings()
