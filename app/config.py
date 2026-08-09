import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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

    def provider_key(self, name: str) -> str:
        key = _env(f"GATEWAY_{name.upper()}_KEY")
        if name == "huggingface" and not key:
            key = self.HF_KEY
        return key


settings = Settings()
