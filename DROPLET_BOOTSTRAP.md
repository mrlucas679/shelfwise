# MI300X Droplet Bootstrap

Create the single-GPU **MI300X** plan with the **vLLM 0.23.0 Quick Start** image. It has 192 GB
VRAM, 720 GB boot disk, and avoids the unnecessary eight-GPU hourly rate. The host must expose
`/dev/kfd`, `/dev/dri`, at least 150 GB free disk,
and a firewall that allows only the ShelfWise application host to reach TCP `8000` and `8001`.
Do not expose either vLLM port broadly on the internet.

The bootstrap installs Docker and `curl` itself on a root apt-based Ubuntu image when they are
absent. It still fails fast if the chosen droplet does not expose the AMD ROCm devices.

On the droplet, clone the pushed `developers` branch and run the bootstrap. `HF_TOKEN` must belong
to an account that has accepted both Gemma model licences. `VLLM_API_KEY` is the non-empty secret
the ShelfWise backend sends as its bearer token; generate it locally and do not put it in Git.

```bash
git clone --branch developers https://github.com/mrlucas679/shelfwise.git /opt/shelfwise
cd /opt/shelfwise
export HF_TOKEN='<Hugging Face token with Gemma access>'
export VLLM_API_KEY="$(openssl rand -hex 32)"
printf '%s\n' "$VLLM_API_KEY" > /root/shelfwise-vllm-api-key
chmod 600 /root/shelfwise-vllm-api-key
bash scripts/bootstrap_mi300x_vllm.sh
```

The script detects the Quick Start's preinstalled `rocm` vLLM 0.23 container and starts both
models inside it, avoiding an incompatible duplicate image. It downloads E4B routine and 31B strong
models into the Hugging Face cache, starts them on `8000` and `8001`, then waits for `/v1/models` to
prove each model is loaded. It is safe to rerun; it only replaces its named vLLM processes. On a
non-Quick-Start host it falls back to the official Gemma 4 ROCm vLLM image.

On the application host, set the printed URLs and the same `VLLM_API_KEY` before starting the
production Compose stack. Then run:

```powershell
python scripts/track3_prescreen.py `
  --base-url http://<public-shelfwise-origin> `
  --startup-deadline 60 `
  --request-deadline 29 `
  --output reports/track3_prescreen.json
```

The official vLLM Gemma 4 guide lists both E4B and 31B as supported on one MI300X and documents
the ROCm Docker image, native Gemma tool parser, and server flags used by this script.
