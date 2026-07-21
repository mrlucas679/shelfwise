from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mi300x_bootstrap_starts_distinct_authenticated_gemma_tiers() -> None:
    source = (ROOT / "scripts" / "bootstrap_mi300x_vllm.sh").read_text(encoding="utf-8")

    assert "google/gemma-4-E4B-it" in source
    assert "google/gemma-4-31B-it" in source
    assert "vllm/vllm-openai-rocm:gemma4" in source
    assert "--tool-call-parser gemma4" in source
    assert "--enable-auto-tool-choice" in source
    assert "--api-key" in source
    assert "ROUTINE_PORT:-8000" in source
    assert "STRONG_PORT:-8001" in source
    assert "/v1/models" in source
    assert "--device=/dev/kfd" in source
    assert "apt-get install -y -qq docker.io curl" in source
    assert "VLLM_HOST_CONTAINER:-rocm" in source
    assert "start_quick_start_server" in source
    assert "ensure_quick_start_container_running" in source
    assert "publish_quick_start_port" in source
    assert "restrict_host_network_port" in source
    assert (
        'while iptables -C INPUT -s "$VLLM_ALLOWED_CIDR" -p tcp --dport "$port" -j ACCEPT'
        in source
    )
    assert 'while iptables -C INPUT -p tcp --dport "$port" -j DROP' in source
    assert 'iptables -D INPUT -s "$VLLM_ALLOWED_CIDR" -p tcp --dport "$port" -j ACCEPT' in source
    assert 'iptables -D INPUT -p tcp --dport "$port" -j DROP' in source
    assert 'iptables -I INPUT 1 -p tcp --dport "$port" -j DROP' in source
    assert 'iptables -I INPUT 1 -s "$VLLM_ALLOWED_CIDR" -p tcp --dport "$port" -j ACCEPT' in source
    assert "iptables -t nat" in source
    assert 'VLLM_ALLOWED_CIDR="${VLLM_ALLOWED_CIDR:-}"' in source
    assert "VLLM_ALLOWED_CIDR is required" in source
    assert "ipaddress.ip_network" in source
    assert source.count('-s "$VLLM_ALLOWED_CIDR"') >= 4
    assert "Remove rules from older bootstrap versions" in source
    assert "wait_for_model \"$ROUTINE_PORT\"" in source
    assert "pgrep -f '[v]llm serve" in source
    assert "model download and ROCm warmup can take several minutes" in source
    assert "docker exec \"$VLLM_HOST_CONTAINER\" curl" in source
    assert "[v]llm serve" in source
    assert "--disable-log-requests" not in source
    assert "--enforce-eager" in source


def test_droplet_bootstrap_docs_keep_required_secrets_out_of_git() -> None:
    source = (ROOT / "DROPLET_BOOTSTRAP.md").read_text(encoding="utf-8")

    assert "HF_TOKEN" in source
    assert "VLLM_API_KEY" in source
    assert "track3_prescreen.py" in source
    assert "developers" in source
    assert "SHELFWISE_COOKIE_SECURE=true" in source
    assert "SHELFWISE_ALLOW_INSECURE_COOKIE_IN_DISPOSABLE_CI=true" in source
    assert "non-allowlisted host" in source
    assert "old key fails" in source
    assert "```powershell\npython scripts/track3_prescreen.py" not in source
    assert "```bash\npython scripts/track3_prescreen.py " in source
