import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_test2.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"
os.environ["HF_TOKEN"] = os.popen("hf auth token 2>/dev/null").read().strip()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

print("health:", client.get("/health").json())

r = client.post("/v1/keys", json={"name": "acme-corp"}, headers={"x-admin-key": "testadmin"})
assert r.status_code == 200, r.text
skey = r.json()["skey"]
print("created key:", skey[:12] + "...")

print("models:", [m["id"] for m in client.get("/v1/models", headers={"Authorization": f"Bearer {skey}"}).json()["data"]][:5])

# bad key rejected
assert client.get("/v1/usage", headers={"Authorization": "Bearer gw_bad"}).status_code == 401
print("auth: invalid key rejected OK")

# real inference through HF router
body = {
    "model": "llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Reply with exactly: gateway works"}],
    "max_tokens": 20,
}
r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {skey}"})
print("chat status:", r.status_code)
if r.status_code == 200:
    d = r.json()
    print("reply:", d["choices"][0]["message"]["content"])
    print("usage:", d["usage"])
else:
    print("body:", r.text[:400])

print("usage endpoint:", client.get("/v1/usage", headers={"Authorization": f"Bearer {skey}"}).json())
print("admin usage:", client.get("/v1/admin/usage", headers={"x-admin-key": "testadmin"}).json()["usage"])
print("ALL TESTS PASSED" if r.status_code == 200 else "INFERENCE FAILED")
