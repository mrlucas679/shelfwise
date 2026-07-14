#!/usr/bin/env bash
# Provision the two Tier-3 Gemma 4 vLLM servers on one AMD MI300X host.
# Required: HF_TOKEN with accepted Gemma licence and a non-empty VLLM_API_KEY.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly ROUTINE_CONTAINER="${ROUTINE_CONTAINER:-shelfwise-vllm-routine}"
readonly STRONG_CONTAINER="${STRONG_CONTAINER:-shelfwise-vllm-strong}"
readonly ROUTINE_PORT="${ROUTINE_PORT:-8000}"
readonly STRONG_PORT="${STRONG_PORT:-8001}"
readonly ROUTINE_MODEL="${ROUTINE_MODEL:-google/gemma-4-E4B-it}"
readonly STRONG_MODEL="${STRONG_MODEL:-google/gemma-4-31B-it}"
readonly VLLM_ALLOWED_CIDR="${VLLM_ALLOWED_CIDR:-}"
# The official Gemma 4 ROCm image tag is the compatibility baseline. Override only after
# validating the replacement in a disposable environment.
readonly VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai-rocm:gemma4}"
readonly VLLM_HOST_CONTAINER="${VLLM_HOST_CONTAINER:-rocm}"
readonly HF_CACHE_DIR="${HF_CACHE_DIR:-$HOME/.cache/huggingface}"
readonly STARTUP_TIMEOUT_SECONDS="${STARTUP_TIMEOUT_SECONDS:-900}"
readonly APP_VENV_DIR="${APP_VENV_DIR:-/opt/shelfwise/.venv}"
readonly BOOTSTRAP_RECEIPT="${BOOTSTRAP_RECEIPT:-/root/shelfwise-mi300x-bootstrap.json}"

validate_numeric_settings() {
  # Reject malformed ports and timeouts before any package install or model download.
  for setting in ROUTINE_PORT STRONG_PORT STARTUP_TIMEOUT_SECONDS; do
    local value="${!setting}"
    [[ "$value" =~ ^[0-9]+$ ]] || {
      echo "$setting must be an integer" >&2
      exit 1
    }
  done
  (( ROUTINE_PORT >= 1 && ROUTINE_PORT <= 65535 )) || {
    echo "ROUTINE_PORT must be between 1 and 65535" >&2
    exit 1
  }
  (( STRONG_PORT >= 1 && STRONG_PORT <= 65535 )) || {
    echo "STRONG_PORT must be between 1 and 65535" >&2
    exit 1
  }
  (( STARTUP_TIMEOUT_SECONDS >= 30 && STARTUP_TIMEOUT_SECONDS <= 3600 )) || {
    echo "STARTUP_TIMEOUT_SECONDS must be between 30 and 3600" >&2
    exit 1
  }
}

ensure_host_dependencies() {
  # Keep the droplet preflight deterministic even when the provider image is minimal.
  local needs_install=0
  for command in docker curl iptables openssl git python3 awk hostname; do
    if ! command -v "$command" >/dev/null 2>&1; then
      needs_install=1
      break
    fi
  done
  if (( needs_install )); then
    command -v apt-get >/dev/null || {
      echo "missing host tools; automatic installation supports apt-based hosts only" >&2
      exit 1
    }
    [[ "$(id -u)" == "0" ]] || {
      echo "run as root so the bootstrap can install missing host tools" >&2
      exit 1
    }
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq docker.io curl iptables openssl git python3 python3-venv python3-pip
  fi
  for command in docker curl iptables openssl git python3 awk hostname; do
    command -v "$command" >/dev/null 2>&1 || {
      echo "required host command is missing: $command" >&2
      exit 1
    }
  done
}

require_prerequisites() {
  # Fail before downloading model weights when host or secret prerequisites are missing.
  validate_numeric_settings
  ensure_host_dependencies
  [[ -n "${HF_TOKEN:-}" ]] || { echo "HF_TOKEN is required for gated Gemma model download" >&2; exit 1; }
  [[ -n "${VLLM_API_KEY:-}" ]] || { echo "VLLM_API_KEY is required; do not expose an unauthenticated vLLM API" >&2; exit 1; }
  [[ -e /dev/kfd && -d /dev/dri ]] || {
    echo "AMD ROCm devices /dev/kfd and /dev/dri are required; choose an MI300X ROCm image" >&2
    exit 1
  }
  [[ "$ROUTINE_PORT" != "$STRONG_PORT" ]] || { echo "routine and strong ports must differ" >&2; exit 1; }
}

validate_vllm_allowed_cidr() {
  # iptables needs an IPv4 CIDR that identifies only the application host or its private subnet.
  [[ -n "$VLLM_ALLOWED_CIDR" ]] || {
    echo "VLLM_ALLOWED_CIDR is required; refuse to publish vLLM ports broadly" >&2
    exit 1
  }
  command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required to validate VLLM_ALLOWED_CIDR" >&2
    exit 1
  }
  VLLM_ALLOWED_CIDR="$VLLM_ALLOWED_CIDR" python3 - <<'PY'
import ipaddress
import os
import sys

value = os.environ["VLLM_ALLOWED_CIDR"]
try:
    network = ipaddress.ip_network(value, strict=False)
except ValueError:
    print("VLLM_ALLOWED_CIDR must be a valid IPv4 CIDR", file=sys.stderr)
    raise SystemExit(1)
if network.version != 4 or "/" not in value or network.prefixlen == 0:
    print("VLLM_ALLOWED_CIDR must be a non-default IPv4 CIDR", file=sys.stderr)
    raise SystemExit(1)
PY
}

ensure_application_runtime() {
  # The model container serves inference only; install ShelfWise control-plane tools on the host.
  command -v python3 >/dev/null 2>&1 || {
    command -v apt-get >/dev/null || {
      echo "python3 is required to install ShelfWise host tooling" >&2
      exit 1
    }
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip
  }
  if [[ ! -x "$APP_VENV_DIR/bin/python" ]]; then
    python3 -m venv "$APP_VENV_DIR" || {
      command -v apt-get >/dev/null || {
        echo "python3-venv is required to create the ShelfWise host environment" >&2
        exit 1
      }
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq python3-venv python3-pip
      python3 -m venv "$APP_VENV_DIR"
    }
  fi
  "$APP_VENV_DIR/bin/python" -m pip install --quiet --editable "${REPO_ROOT}[benchmark]"
}

ensure_docker_daemon() {
  # Start Docker when systemd is available, then fail with a useful diagnostic if it is not ready.
  if ! docker info >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl enable --now docker >/dev/null 2>&1 || true
    fi
  fi
  docker info >/dev/null || {
    echo "Docker daemon is not ready; inspect: systemctl status docker --no-pager" >&2
    exit 1
  }
}

remove_existing_container() {
  # Replace a stale named server so the requested image and model configuration is authoritative.
  local name="$1"
  if docker container inspect "$name" >/dev/null 2>&1; then
    docker rm -f "$name" >/dev/null
  fi
}

use_quick_start_container() {
  # AMD's vLLM 0.23 Quick Start image supplies this container with ROCm already matched to the host.
  docker container inspect "$VLLM_HOST_CONTAINER" >/dev/null 2>&1
}

ensure_quick_start_container_running() {
  # Docker treats starting an already-running container as an error, so inspect first.
  if [[ "$(docker inspect --format '{{.State.Running}}' "$VLLM_HOST_CONTAINER")" != "true" ]]; then
    docker start "$VLLM_HOST_CONTAINER" >/dev/null
  fi
}

publish_quick_start_port() {
  # The Quick Start container publishes only its default port; map the strong tier explicitly.
  local port="$1"
  local container_ip
  container_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$VLLM_HOST_CONTAINER")"
  [[ -n "$container_ip" ]] || { echo "unable to resolve Quick Start container IP" >&2; exit 1; }
  # Remove rules from older bootstrap versions so a rerun cannot leave a broad rule active.
  while iptables -t nat -C DOCKER ! -i docker0 -p tcp --dport "$port" \
    -j DNAT --to-destination "$container_ip:$port" 2>/dev/null; do
    iptables -t nat -D DOCKER ! -i docker0 -p tcp --dport "$port" \
      -j DNAT --to-destination "$container_ip:$port"
  done
  if ! iptables -t nat -C DOCKER -s "$VLLM_ALLOWED_CIDR" ! -i docker0 -p tcp --dport "$port" \
    -j DNAT --to-destination "$container_ip:$port" 2>/dev/null; then
    iptables -t nat -I DOCKER 1 -s "$VLLM_ALLOWED_CIDR" ! -i docker0 -p tcp --dport "$port" \
      -j DNAT --to-destination "$container_ip:$port"
  fi
  while iptables -C DOCKER -d "$container_ip/32" ! -i docker0 -o docker0 -p tcp \
    --dport "$port" -j ACCEPT 2>/dev/null; do
    iptables -D DOCKER -d "$container_ip/32" ! -i docker0 -o docker0 -p tcp \
      --dport "$port" -j ACCEPT
  done
  if ! iptables -C DOCKER -s "$VLLM_ALLOWED_CIDR" -d "$container_ip/32" ! -i docker0 -o docker0 -p tcp \
    --dport "$port" -j ACCEPT 2>/dev/null; then
    iptables -I DOCKER 1 -s "$VLLM_ALLOWED_CIDR" -d "$container_ip/32" ! -i docker0 -o docker0 -p tcp \
      --dport "$port" -j ACCEPT
  fi
}

start_quick_start_server() {
  # Start one server inside the provider's preinstalled vLLM container without replacing its image.
  local model="$1"
  local port="$2"
  local memory_fraction="$3"
  ensure_quick_start_container_running
  publish_quick_start_port "$port"
  docker exec "$VLLM_HOST_CONTAINER" bash -lc \
    "pkill -f '[v]llm serve.*--port ${port}' >/dev/null 2>&1 || true"
  docker exec -d \
    -e "VLLM_API_KEY=$VLLM_API_KEY" \
    -e "HF_TOKEN=$HF_TOKEN" \
    -e "HUGGING_FACE_HUB_TOKEN=$HF_TOKEN" \
    "$VLLM_HOST_CONTAINER" \
    bash -lc \
    'mkdir -p /root/shelfwise-vllm; nohup vllm serve "$1" --host 0.0.0.0 --port "$2" --api-key "$VLLM_API_KEY" --dtype bfloat16 --max-model-len 8192 --gpu-memory-utilization "$3" --enforce-eager --enable-auto-tool-choice --tool-call-parser gemma4 > "/root/shelfwise-vllm/vllm-$2.log" 2>&1 &' \
    bash "$model" "$port" "$memory_fraction"
  sleep 3
  docker exec "$VLLM_HOST_CONTAINER" bash -lc \
    "pgrep -f '[v]llm serve.*--port ${port}' >/dev/null || { cat /root/shelfwise-vllm/vllm-${port}.log; exit 1; }"
}

start_server() {
  # Launch one constrained vLLM server sharing the MI300X safely with the other tier.
  local name="$1"
  local model="$2"
  local port="$3"
  local memory_fraction="$4"

  if use_quick_start_container; then
    start_quick_start_server "$model" "$port" "$memory_fraction"
    return 0
  fi

  remove_existing_container "$name"
  docker run -d \
    --name "$name" \
    --restart unless-stopped \
    --ipc=host \
    --network=host \
    --privileged \
    --cap-add=CAP_SYS_ADMIN \
    --cap-add=SYS_PTRACE \
    --device=/dev/kfd \
    --device=/dev/dri \
    --group-add=video \
    --security-opt=seccomp=unconfined \
    --shm-size=16g \
    -e "HF_TOKEN=$HF_TOKEN" \
    -e "HUGGING_FACE_HUB_TOKEN=$HF_TOKEN" \
    -v "$HF_CACHE_DIR:/root/.cache/huggingface" \
    "$VLLM_IMAGE" \
    --model "$model" \
    --host 0.0.0.0 \
    --port "$port" \
    --api-key "$VLLM_API_KEY" \
    --dtype bfloat16 \
    --max-model-len 8192 \
    --gpu-memory-utilization "$memory_fraction" \
    --enforce-eager \
    --limit-mm-per-prompt image=0,audio=0 \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    --trust-remote-code
}

wait_for_model() {
  # Wait for vLLM readiness and confirm the expected model was actually loaded.
  local port="$1"
  local model="$2"
  local container="$3"
  local deadline=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
  local response=""

  echo "Waiting for $model on port $port; model download and ROCm warmup can take several minutes."
  until (( SECONDS >= deadline )); do
    if use_quick_start_container; then
      response="$(docker exec "$VLLM_HOST_CONTAINER" curl --fail --silent --show-error \
        --max-time 10 -H "Authorization: Bearer $VLLM_API_KEY" \
        "http://127.0.0.1:$port/v1/models" 2>/dev/null || true)"
    else
      response="$(curl --fail --silent --show-error --max-time 10 \
        -H "Authorization: Bearer $VLLM_API_KEY" "http://127.0.0.1:$port/v1/models" 2>/dev/null || true)"
    fi
    if [[ -n "$response" ]] && MODEL_EXPECTED="$model" RESPONSE="$response" python3 - <<'PY'
import json
import os

try:
    payload = json.loads(os.environ["RESPONSE"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit(1)
models = payload.get("data", []) if isinstance(payload, dict) else []
expected = os.environ.get("MODEL_EXPECTED")
raise SystemExit(0 if any(item.get("id") == expected for item in models if isinstance(item, dict)) else 1)
PY
    then
      echo "$container ready on port $port with $model"
      return 0
    fi
    if use_quick_start_container; then
      docker exec "$VLLM_HOST_CONTAINER" bash -lc \
        "tail -3 /root/shelfwise-vllm/vllm-${port}.log 2>/dev/null || true"
    else
      docker logs --tail 3 "$container" 2>&1 || true
    fi
    sleep 5
  done

  echo "$container did not become ready within ${STARTUP_TIMEOUT_SECONDS}s" >&2
  if use_quick_start_container; then
    docker exec "$VLLM_HOST_CONTAINER" bash -lc \
      "tail -200 /root/shelfwise-vllm/vllm-${port}.log 2>/dev/null || true" >&2
  else
    docker logs --tail 200 "$container" >&2 || true
  fi
  return 1
}

write_bootstrap_receipt() {
  # Persist a secret-free proof of the exact code, model, port, and host configuration used.
  local commit branch dirty version host_ip
  commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  branch="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo unknown)"
  dirty="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | head -1)"
  host_ip="$(hostname -I | awk '{print $1}')"
  version="unknown"
  if use_quick_start_container; then
    version="$(docker exec "$VLLM_HOST_CONTAINER" vllm --version 2>/dev/null || echo unknown)"
  fi
  mkdir -p "$(dirname -- "$BOOTSTRAP_RECEIPT")"
  umask 077
  BOOTSTRAP_RECEIPT="$BOOTSTRAP_RECEIPT" \
  REPO_ROOT="$REPO_ROOT" \
  REPO_COMMIT="$commit" \
  REPO_BRANCH="$branch" \
  REPO_DIRTY="$dirty" \
  HOST_IP="$host_ip" \
  VLLM_HOST_CONTAINER="$VLLM_HOST_CONTAINER" \
  ROUTINE_CONTAINER="$ROUTINE_CONTAINER" \
  STRONG_CONTAINER="$STRONG_CONTAINER" \
  VLLM_ALLOWED_CIDR="$VLLM_ALLOWED_CIDR" \
  VLLM_IMAGE="$VLLM_IMAGE" \
  VLLM_VERSION="$version" \
  HF_CACHE_DIR="$HF_CACHE_DIR" \
  APP_VENV_DIR="$APP_VENV_DIR" \
  ROUTINE_MODEL="$ROUTINE_MODEL" \
  ROUTINE_PORT="$ROUTINE_PORT" \
  STRONG_MODEL="$STRONG_MODEL" \
  STRONG_PORT="$STRONG_PORT" \
  python3 - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

def integer(name: str) -> int:
    return int(os.environ[name])

receipt = {
    "schema_version": "mi300x-bootstrap/v1",
    "generated_at": datetime.now(UTC).isoformat(),
    "repo": {
        "path": os.environ["REPO_ROOT"],
        "branch": os.environ["REPO_BRANCH"],
        "commit": os.environ["REPO_COMMIT"],
        "dirty": bool(os.environ["REPO_DIRTY"]),
    },
    "host": {"private_ip_observed": os.environ["HOST_IP"]},
    "containers": {
        "quick_start": os.environ["VLLM_HOST_CONTAINER"],
        "routine": os.environ["ROUTINE_CONTAINER"],
        "strong": os.environ["STRONG_CONTAINER"],
    },
    "hf_cache_dir": os.environ["HF_CACHE_DIR"],
    "app_venv_dir": os.environ["APP_VENV_DIR"],
    "vllm_image": os.environ["VLLM_IMAGE"],
    "vllm_version": os.environ["VLLM_VERSION"],
    "allowed_cidr": os.environ["VLLM_ALLOWED_CIDR"],
    "models": [
        {
            "tier": "routine",
            "model": os.environ["ROUTINE_MODEL"],
            "port": integer("ROUTINE_PORT"),
            "gpu_memory_utilization": 0.20,
            "ready": True,
        },
        {
            "tier": "strong",
            "model": os.environ["STRONG_MODEL"],
            "port": integer("STRONG_PORT"),
            "gpu_memory_utilization": 0.55,
            "ready": True,
        },
    ],
}
path = Path(os.environ["BOOTSTRAP_RECEIPT"])
path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  echo "Bootstrap receipt: $BOOTSTRAP_RECEIPT"
}

print_application_configuration() {
  # Print non-secret environment assignments for the ShelfWise application host.
  local host_ip
  host_ip="$(hostname -I | awk '{print $1}')"
  cat <<EOF

Both MI300X vLLM servers are ready.

On the ShelfWise application host, set these values (supply VLLM_API_KEY separately):
export LLM_ROUTINE_BASE_URL=http://${host_ip}:${ROUTINE_PORT}
export LLM_STRONG_BASE_URL=http://${host_ip}:${STRONG_PORT}
export LLM_ROUTINE_MODEL=${ROUTINE_MODEL}
export LLM_STRONG_MODEL=${STRONG_MODEL}
export LLM_COMPUTE_RESOURCE="AMD Developer Cloud"
export LLM_ACCELERATOR="AMD Instinct MI300X"
export LLM_TIMEOUT_SECONDS=25

Verify with: curl -H "Authorization: Bearer <VLLM_API_KEY>" http://${host_ip}:${ROUTINE_PORT}/v1/models
EOF
}

main() {
  # Provision both model tiers, then prove their model endpoints.
  cd "$REPO_ROOT"
  require_prerequisites
  ensure_docker_daemon
  validate_vllm_allowed_cidr
  ensure_application_runtime
  mkdir -p "$HF_CACHE_DIR"
  if use_quick_start_container; then
    echo "Using preinstalled AMD vLLM Quick Start container: $VLLM_HOST_CONTAINER"
    ensure_quick_start_container_running
    docker exec "$VLLM_HOST_CONTAINER" vllm --version
  else
    echo "No preinstalled vLLM Quick Start container found; pulling $VLLM_IMAGE"
    docker pull "$VLLM_IMAGE"
  fi
  # A 192GB MI300X has room for both models plus bounded KV cache; do not raise these independently.
  start_server "$ROUTINE_CONTAINER" "$ROUTINE_MODEL" "$ROUTINE_PORT" "0.20"
  wait_for_model "$ROUTINE_PORT" "$ROUTINE_MODEL" "$ROUTINE_CONTAINER"
  start_server "$STRONG_CONTAINER" "$STRONG_MODEL" "$STRONG_PORT" "0.55"
  wait_for_model "$STRONG_PORT" "$STRONG_MODEL" "$STRONG_CONTAINER"
  write_bootstrap_receipt
  print_application_configuration
}

main "$@"
