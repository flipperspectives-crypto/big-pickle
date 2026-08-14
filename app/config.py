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
    # x402 machine-payable top-up (x402 v2, EIP-3009 USDC).
    # Network configuration is centralized and driven by X402_NETWORK_MODE so the
    # two environments can never be mixed by accident. Default is TESTNET; mainnet
    # is opt-in and FAILS CLOSED unless explicitly enabled with credentials.
    X402_ENABLED: bool = _env("X402_ENABLED", "false").lower() in ("1", "true", "yes")
    X402_PAYTO: str = _env("X402_PAYTO", "")  # gateway receiving wallet (public address)
    X402_PRICE_USD: str = _env("X402_PRICE_USD", "0.001")
    # Canonical public origin used to build ABSOLUTE x402 resource URLs in the
    # PAYMENT-REQUIRED challenge. External validators (e.g. Coinbase x402 Bazaar)
    # require resource.url to be a full https:// URL. We source this from a fixed
    # configured origin rather than the request Host/X-Forwarded-Host header, which
    # a proxy or client could spoof. Defaults to the deployed Clarity funnel host.
    X402_PUBLIC_ORIGIN: str = _env(
        "X402_PUBLIC_ORIGIN", "https://desktop-o99r0sf.tail935fba.ts.net"
    ).rstrip("/")
    # Solana DEVNET x402 direct-inference path (separate from Base EVM). This is
    # DEVNET-ONLY: it must never be pointed at Solana mainnet. The receiving
    # address is a PUBLIC wallet address only -- no private key is ever stored,
    # requested, or used server-side.
    X402_SOLANA_ENABLED: bool = _env("X402_SOLANA_ENABLED", "false").lower() in (
        "1", "true", "yes"
    )
    X402_SOLANA_PAYTO: str = _env("X402_SOLANA_PAYTO", "")  # PUBLIC Solana address only
    X402_SOLANA_NETWORK: str = _env(
        "X402_SOLANA_NETWORK", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
    )
    X402_SOLANA_FACILITATOR_URL: str = _env(
        "X402_SOLANA_FACILITATOR_URL", "https://x402.org/facilitator"
    )

    # Fail-closed guardrails for the Solana devnet path. It must NEVER be mainnet,
    # and a receiving address is mandatory when enabled. The address shape is
    # validated inline (base58, 32-44 chars) without importing the x402 SVM
    # packages, so config loading never requires Solana dependencies on Base-only
    # hosts.
    _SVM_ADDRESS_REGEX = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
    _SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKvdp"

    def __init__(self, **overrides):
        # Permit programmatic overrides (tests, future dynamic reconfiguration)
        # while re-running the fail-closed Solana devnet guardrails on EVERY
        # construction -- not only at import time when the class body executes,
        # so a Solana mainnet CAIP-2 value can never be accepted.
        for key, value in overrides.items():
            setattr(self, key, value)
        self._validate_solana_guardrails()

    def _validate_solana_guardrails(self):
        if not self.X402_SOLANA_ENABLED:
            return
        if (
            self.X402_SOLANA_NETWORK == self._SOLANA_MAINNET_CAIP2
            or "mainnet" in self.X402_SOLANA_NETWORK.lower()
        ):
            raise RuntimeError(
                "X402_SOLANA_NETWORK must be Solana DEVNET "
                f"({self.X402_SOLANA_NETWORK!r}); Solana mainnet is not permitted."
            )
        if not self.X402_SOLANA_PAYTO or not __import__("re").match(
            self._SVM_ADDRESS_REGEX, self.X402_SOLANA_PAYTO
        ):
            raise RuntimeError(
                "X402_SOLANA_ENABLED is true but X402_SOLANA_PAYTO is missing or not a "
                "valid public Solana address (base58, 32-44 chars). Refusing to start."
            )

    _X402_TESTNET = {
        "chain_id": "eip155:84532",
        "chain_int": 84532,
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
        "facilitator": "https://x402.org/facilitator",
        "rpc": "https://sepolia.base.org",
    }
    _X402_MAINNET = {
        "chain_id": "eip155:8453",
        "chain_int": 8453,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "facilitator": "https://api.cdp.coinbase.com/platform/v2/x402",
        "rpc": "https://mainnet.base.org",
    }
    X402_NETWORK_MODE: str = _env("X402_NETWORK_MODE", "testnet").strip().lower()
    if X402_NETWORK_MODE not in ("testnet", "mainnet"):
        # Fail closed: an unrecognized mode must never silently pick a network.
        raise RuntimeError(
            "X402_NETWORK_MODE must be 'testnet' or 'mainnet' "
            f"(got {X402_NETWORK_MODE!r})"
        )
    _X402_CFG = _X402_MAINNET if X402_NETWORK_MODE == "mainnet" else _X402_TESTNET
    X402_CHAIN_ID: str = _X402_CFG["chain_id"]
    X402_CHAIN_INT: int = _X402_CFG["chain_int"]
    X402_ASSET: str = _X402_CFG["asset"]
    X402_FACILITATOR_URL: str = _X402_CFG["facilitator"]
    X402_RPC_URL: str = _X402_CFG["rpc"]
    # CDP mainnet facilitator auth (secret). Only used when X402_NETWORK_MODE=mainnet.
    X402_CDP_API_KEY_ID: str = _env("CDP_API_KEY_ID", "")
    X402_CDP_API_KEY_SECRET: str = _env("CDP_API_KEY_SECRET", "")
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
