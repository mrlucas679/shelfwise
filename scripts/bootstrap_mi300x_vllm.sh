#!/usr/bin/env bash
# Provision the two Tier-3 Gemma 4 vLLM servers on one AMD MI300X host.
# Required: HF_TOKEN with accepted Gemma licence and a non-empty VLLM_API_KEY.
set -euo pipefail

readonly ROUTINE_CONTAINER="${ROUTINE_CONTAINER:-shelfwise-vllm-routine}"
readonly STRONG_CONTAINER="${STRONG_CONTAINER:-shelfwise-vllm-strong}"
readonly ROUTINE_PORT="${ROUTINE_PORT:-8000}"
readonly STRONG_PORT="${STRONG_PORT:-8001}"
readonly ROUTINE_MODEL="${ROUTINE_MODEL:-google/gemma-4-E4B-it}"
readonly STRONG_MODEL="${STRONG_MODEL:-google/gemma-4-31B-it}"
# The official Gemma 4 ROCm image tag is the compatibility baseline. Override only after
# validating the replacement in a disposable environment.
readonly VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai-rocm:gemma4}"
readonly VLLM_HOST_CONTAINER="${VLLM_HOST_CONTAINER:-rocm}"
readonly HF_CACHE_DIR="${HF_CACHE_DIR:-$HOME/.cache/huggingface}"
readonly STARTUP_TIMEOUT_SECONDS="${STARTUP_TIMEOUT_SECONDS:-900}"

require_prerequisites() {
  # Fail before downloading model weights when host or secret prerequisites are missing.
  ensure_container_runtime
  [[ -n "${HF_TOKEN:-}" ]] || { echo "HF_TOKEN is required for gated Gemma model download" >&2; exit 1; }
  [[ -n "${VLLM_API_KEY:-}" ]] || { echo "VLLM_API_KEY is required; do not expose an unauthenticated vLLM API" >&2; exit 1; }
  [[ -e /dev/kfd && -d /dev/dri ]] || {
    echo "AMD ROCm devices /dev/kfd and /dev/dri are required; choose an MI300X ROCm image" >&2
    exit 1
  }
  [[ "$ROUTINE_PORT" != "$STRONG_PORT" ]] || { echo "routine and strong ports must differ" >&2; exit 1; }
}

ensure_container_runtime() {
  # Install the small host runtime dependency set on a standard root Ubuntu droplet if needed.
  if ! command -v docker >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    command -v apt-get >/dev/null || {
      echo "docker and curl are required; automatic installation supports apt-based hosts only" >&2
      exit 1
    }
    [[ "$(id -u)" == "0" ]] || {
      echo "run as root so the bootstrap can install Docker when the base image lacks it" >&2
      exit 1
    }
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq docker.io curl
    systemctl enable --now docker
  fi
  docker info >/dev/null || { echo "Docker daemon is not ready" >&2; exit 1; }
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

start_quick_start_server() {
  # Start one server inside the provider's preinstalled vLLM container without replacing its image.
  local model="$1"
  local port="$2"
  local memory_fraction="$3"
  ensure_quick_start_container_running
  docker exec "$VLLM_HOST_CONTAINER" bash -lc \
    "pkill -f '[v]llm serve.*--port ${port}' >/dev/null 2>&1 || true"
  docker exec -d \
    -e "VLLM_API_KEY=$VLLM_API_KEY" \
    "$VLLM_HOST_CONTAINER" \
    bash -lc \
    'mkdir -p /root/shelfwise-vllm; nohup vllm serve "$1" --host 0.0.0.0 --port "$2" --api-key "$VLLM_API_KEY" --dtype bfloat16 --max-model-len 8192 --gpu-memory-utilization "$3" --enable-auto-tool-choice --tool-call-parser gemma4 > "/root/shelfwise-vllm/vllm-$2.log" 2>&1 &' \
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
    if response="$(curl --fail --silent --show-error --max-time 10 \
      -H "Authorization: Bearer $VLLM_API_KEY" "http://127.0.0.1:$port/v1/models" 2>/dev/null)"; then
      if grep --fixed-strings --quiet "$model" <<<"$response"; then
        echo "$container ready on port $port with $model"
        return 0
      fi
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
  docker logs --tail 200 "$container" >&2 || true
  return 1
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
  require_prerequisites
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
  print_application_configuration
}

main "$@"
