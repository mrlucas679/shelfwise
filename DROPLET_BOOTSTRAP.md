# MI300X Droplet Bootstrap

> **Working-product branch boundary:** The post-hackathon deployment source is the `developers`
> branch. Do not bootstrap or commit this implementation from `main` unless an explicit release
> decision has been made.

Create the single-GPU **MI300X** plan with the **vLLM 0.23.0 Quick Start** image. It has 192 GB
VRAM, 720 GB boot disk, and avoids the unnecessary eight-GPU hourly rate. The host must expose
`/dev/kfd`, `/dev/dri`, at least 150 GB free disk,
and a firewall that allows only the ShelfWise application host CIDR to reach TCP `8000` and `8001`.
Do not expose either vLLM port broadly on the internet. The bootstrap requires the same source
allowlist in `VLLM_ALLOWED_CIDR` and applies it to host iptables before model startup.

The bootstrap installs Docker and `curl` itself on a root apt-based Ubuntu image when they are
absent. It still fails fast if the chosen droplet does not expose the AMD ROCm devices.
It also validates the ports and allowlist before model downloads, resolves the repository root
from the script location, and writes a secret-free receipt to
`/root/shelfwise-mi300x-bootstrap.json` after both model checks pass.

On the droplet, clone the pushed `developers` branch and run the bootstrap. `HF_TOKEN` must belong
to an account that has accepted both Gemma model licences. `VLLM_API_KEY` is the non-empty secret
the ShelfWise backend sends as its bearer token; generate it locally and do not put it in Git.

```bash
git clone --branch developers --single-branch https://github.com/mrlucas679/shelfwise.git /opt/shelfwise
cd /opt/shelfwise
git rev-parse HEAD
export HF_TOKEN='<Hugging Face token with Gemma access>'
export VLLM_API_KEY="$(openssl rand -hex 32)"
export VLLM_ALLOWED_CIDR='<application-host-private-ip>/32'
printf '%s\n' "$VLLM_API_KEY" > /root/shelfwise-vllm-api-key
chmod 600 /root/shelfwise-vllm-api-key
bash scripts/bootstrap_mi300x_vllm.sh
```

The script is safe to invoke from any working directory, but run it from `/opt/shelfwise` when
copying commands into an incident record. Preserve the commit printed by `git rev-parse HEAD` and
the bootstrap receipt together; the branch name alone is not a reproducible version identifier.

Immediately after a successful run, capture the non-secret proof:

```bash
cat /root/shelfwise-mi300x-bootstrap.json
docker exec rocm tail -20 /root/shelfwise-vllm/vllm-8000.log
docker exec rocm tail -20 /root/shelfwise-vllm/vllm-8001.log
```

If startup times out, the bootstrap prints the matching vLLM log tail. Do not repeatedly rerun it
before checking that output: a model download, ROCm warmup, rejected Gemma licence, or an out of
memory condition requires a different fix.

The script detects the Quick Start's preinstalled `rocm` vLLM 0.23 container and starts both
models inside it, avoiding an incompatible duplicate image. It downloads E4B routine and 31B strong
models into the Hugging Face cache, starts them on `8000` and `8001`, then waits for `/v1/models` to
prove each model is loaded. It is safe to rerun; it only replaces its named vLLM processes. On a
non-Quick-Start host it falls back to the official Gemma 4 ROCm vLLM image.

On the application host, set the printed URLs and the same `VLLM_API_KEY` before starting the
production Compose stack. Production Compose defaults `SHELFWISE_COOKIE_SECURE=true`; terminate
HTTPS before the frontend Nginx listener and keep the HTTP listener private to that terminator.
Never set `SHELFWISE_COOKIE_SECURE=false` on a real deployment. The only supported exception is a
disposable CI smoke with both `SHELFWISE_COOKIE_SECURE=false` and
`SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI=true` set explicitly.

From the application repository root, validate the production configuration before starting it:

```bash
cp .env.example .env  # only on a new host; keep the file local and ignored
# Set the production secrets and the two printed LLM_* values in .env.
docker compose -f docker-compose.production.yml config --quiet
docker compose -f docker-compose.production.yml up --build -d --wait
python scripts/deployment_shakedown.py \
  --base-url https://<public-shelfwise-origin> \
  --cycles 3 \
  --live-required \
  --output reports/deployment-shakedown.json
```

Keep the model-host receipt and the application shakedown receipt together. A successful
`/readiness` response without both receipts is configuration evidence, not proof that the fresh
droplet and public application are connected end to end.

Configure defense in depth in both places before starting the stack:

- In the cloud firewall, allow TCP `8000` and `8001` only from the application-host CIDR, deny
  other sources, and keep Postgres/Redis private. Allow public HTTPS only at the TLS terminator.
- On the MI300X host, verify the source-restricted rules after bootstrap:

```bash
sudo iptables -t nat -S DOCKER | grep -- '--dport 8000'
sudo iptables -t nat -S DOCKER | grep -- '--dport 8001'
sudo iptables -S DOCKER | grep -- '--dport 8000'
sudo iptables -S DOCKER | grep -- '--dport 8001'
```

From the allowlisted application host, prove authenticated access with a placeholder only:

```bash
curl --fail --silent --show-error \
  -H 'Authorization: Bearer <VLLM_API_KEY>' \
  http://<mi300x-private-ip>:8000/v1/models
```

From a non-allowlisted host, the same request must time out or be refused:

```bash
curl --connect-timeout 5 --fail \
  -H 'Authorization: Bearer <VLLM_API_KEY>' \
  http://<mi300x-private-ip>:8000/v1/models
```

After the public HTTPS terminator is active, verify the browser session response contains
`Secure`, `HttpOnly`, and `SameSite=Strict` on `shelfwise_session`:

```bash
curl --silent --show-error --dump-header - \
  --request POST https://<public-shelfwise-origin>/auth/session
```

Rotate `VLLM_API_KEY` on a schedule and after any suspected exposure: generate a new value,
restart both vLLM tiers with the new key, update the application host environment, restart the
production stack, then confirm the old key fails and the new key succeeds. Keep both values out of
Git, shell history where practical, logs, screenshots, and reports.

Then run:

```powershell
python scripts/track3_prescreen.py `
  --base-url https://<public-shelfwise-origin> `
  --startup-deadline 60 `
  --request-deadline 130 `
  --output reports/track3_prescreen.json
```

The official vLLM Gemma 4 guide lists both E4B and 31B as supported on one MI300X and documents
the ROCm Docker image, native Gemma tool parser, and server flags used by this script.
