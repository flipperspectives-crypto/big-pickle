# Clarity Windows Control Scripts

Source-controlled under `scripts/windows/`. These scripts are installed onto a
Windows host into `%USERPROFILE%\Clarity-Launcher` by
`Install-Clarity-Launchers.ps1`. They are NOT executed by the Linux/CI build;
they are shipped as auditable source for the Windows operator.

## Files

| Script | Purpose |
| ------ | ------- |
| `Start-Clarity.ps1` | Verify repo/venv, ensure Ollama, start Clarity, run readiness gates, write `Last-Start.json`. |
| `Stop-Clarity.ps1` | Stop only the Clarity uvicorn process on port 7860 (by command-line match), leave Ollama running, write `Last-Stop.json`. |
| `Update-Clarity.ps1` | Fast-forward to `origin/main`, then run Start attestation. Success only if runtime `current_commit` matches new HEAD. |
| `Rollback-Clarity.ps1` | Detached-head checkout to the previous commit, run Start attestation. Never touches `origin/main`. |
| `Install-Clarity-Launchers.ps1` | Copy scripts into `Clarity-Launcher`, back up existing scripts, create Desktop shortcuts. |
| `Create-Clarity-Recovery-Bundle.ps1` | Build a timestamped, secret-free recovery archive under `%USERPROFILE%\Clarity-Recovery`. |

## Runtime attestation (Start-Clarity.ps1)

Readiness gates (no inference):

1. `GET /health` => `{"status":"ok"}`
2. `GET /v1/build` => `current_commit == git rev-parse HEAD`
3. `GET /v1/diagnostics` => `gateway.process_healthy == true`
4. `GET /v1/models` => at least one `local:*` model
5. `local:qwen3:1.7b` present

Only after all gates pass is `CLARITY READY` printed and the browser opened.

## Safety guarantees

- No `git reset --hard`, no `git clean`, no `git stash`, no history rewrite.
- Update uses fast-forward only.
- Rollback uses a detached checkout; it never moves `origin/main` or deletes work.
- Evidence JSON files never contain API keys, prompts, responses, DB contents,
  `OLLAMA_BASE_URL`, authorization headers, env vars, or credentials.

## Evidence files

- `Clarity-Launcher\Last-Start.json`
- `Clarity-Launcher\Last-Stop.json`
- `Clarity-Launcher\Last-Update.json`
- `Clarity-Launcher\Last-Rollback.json`
- `Clarity-Launcher\Previous-Clarity-Commit.txt` (rollback target)
- `Clarity-Launcher\Redo-Clarity-Commit.txt` (current SHA before rollback)
