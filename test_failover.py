import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_failover.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

from fastapi.testclient import TestClient  # noqa: E402

from app import router  # noqa: E402
from app.main import app  # noqa: E402
from app.router import UpstreamError  # noqa: E402

client = TestClient(app)

FAKE = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "meta-llama/llama-3.1-8b-instruct",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "failover works"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
}

async def fake_chat_openai(client, provider, payload, stream):
    if provider == "deepinfra":
        raise UpstreamError(500, "deepinfra is down")
    assert provider == "together", f"expected fallback to together, got {provider}"
    return FAKE, 12, 3

router._chat_openai = fake_chat_openai

r = client.post("/v1/keys", json={"name": "failover-test"}, headers={"x-admin-key": "testadmin"})
skey = r.json()["skey"]
kid = r.json()["id"]
client.post("/v1/credits", json={"key_id": kid, "amount": 10.0}, headers={"x-admin-key": "testadmin"})

r = client.post(
    "/v1/chat/completions",
    json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
    headers={"Authorization": f"Bearer {skey}"},
)
print("status:", r.status_code)
d = r.json()
print("reply:", d["choices"][0]["message"]["content"])
print("usage:", d["usage"])
assert r.status_code == 200 and d["choices"][0]["message"]["content"] == "failover works"

usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {skey}"}).json()
print("recorded usage:", usage)
assert usage["prompt_tokens"] == 12 and usage["completion_tokens"] == 3
assert usage["balance_usd"] < 10.0

# zero balance -> 402
r0 = client.post("/v1/keys", json={"name": "nofunds"}, headers={"x-admin-key": "testadmin"})
skey0 = r0.json()["skey"]
r = client.post(
    "/v1/chat/completions",
    json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
    headers={"Authorization": f"Bearer {skey0}"},
)
print("zero-balance status:", r.status_code)
assert r.status_code == 402

# both providers down -> 502
async def fake_all_down(client, provider, payload, stream):
    raise UpstreamError(500, "down")

router._chat_openai = fake_all_down
r = client.post(
    "/v1/chat/completions",
    json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
    headers={"Authorization": f"Bearer {skey}"},
)
print("all-down status:", r.status_code, r.json())
assert r.status_code == 502

print("FAILOVER TESTS PASSED")
