# Gemma 4 Multimodal Training Harness

The repo has a scriptable harness for the Gemma 4 LoRA path. It keeps `patch_dense` and
`embedding_projection` in the LoRA targets so the run does not silently collapse to text-only
adaptation. Audio and video are supported through honest fallbacks when native processor
tensors are unavailable: audio uses transcripts, and video uses sampled frame metadata.

Install training dependencies on the ROCm notebook host:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[training]"
pip install torch --index-url https://download.pytorch.org/whl/rocm7.2
```

After pulling the update on the Jupyter GPU server, run the connected smoke path:

```bash
git pull
bash scripts/jupyter_gemma4_check.sh
bash scripts/jupyter_gemma4_bootstrap.sh
```

That script installs the package, runs the harness tests, runs full GPU preflight, then starts a
gated full shakedown. Override the run name without editing files:

```bash
RUN_NAME=shelfwise-mm-full-8h-002 bash scripts/jupyter_gemma4_bootstrap.sh
```

PowerShell local command prefix when the package is not installed in editable mode:

```powershell
$env:PYTHONPATH="src"
```

## Individual Stages

Preflight only:

```bash
python -m shelfwise.training.preflight --config configs/train_gemma4_multimodal.yaml
```

Smoke train:

```bash
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --max_steps 20 --run_name smoke-mm
```

Full run:

```bash
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --run_name gemma4-mm-8h-001
```

Resume by setting `resume_from_checkpoint` in `configs/train_gemma4_multimodal.yaml` to the
checkpoint path under `runs/gemma4-multimodal/<run>/checkpoints/`.

Eval:

```bash
python -m shelfwise.training.evaluate --config configs/train_gemma4_multimodal.yaml --dry-run
```

Adapter export:

```bash
tar -czf /workspace/shelfwise-gemma-final-adapter.tar.gz -C /workspace/checkpoints/shelfwise-gemma final_adapter
```

Serving/plugin check:

```bash
python -m shelfwise.training.serving_check --config configs/train_gemma4_multimodal.yaml --adapter-path shelfwise-gemma-final-adapter/final_adapter --skip-model-load
```

## Full Shakedown (One Gated Run)

```bash
python -m shelfwise.training.shakedown --config configs/train_gemma4_multimodal.yaml --run_name shelfwise-mm-full-8h-001
```

Runs: `preflight -> simulation dataset build -> smoke train -> full train -> eval -> serving check -> final report`

Generate the simulation dataset only, through the dry-run path:

```bash
python -m shelfwise.training.shakedown --config configs/train_gemma4_multimodal.yaml --run_name dataset-check --dry-run
```

The simulation builder emits canonical multimodal episodes across supply-chain reasoning,
multimodal evidence interpretation, incident simulation, report/action planning, and structured
tool-call behavior. It covers damaged goods, missing stock, supplier delays, fake POD, warehouse
voice transcripts, screenshots, proof-of-delivery mismatches, product quality failures, inventory
reconciliation, high-risk supplier patterns, safe cases, and ambiguous missing-evidence cases.

Resume from a checkpoint by setting `resume_from_checkpoint` in
`configs/train_gemma4_multimodal.yaml`, then rerun the shakedown command with a new `--run_name`.

Outputs land under `runs/gemma4-multimodal/` with timestamped checkpoints and reports. The quick
check validates dependencies and fixture generation, but only a generated live-model evaluation
and serving probe can mark a deployment ready.

## Troubleshooting

- Missing target modules: preflight fails if `patch_dense` or `embedding_projection` are absent
  unless `allow_missing_multimodal_targets` is explicitly set.
- Processor load failure: update `transformers`; Gemma 4 uses `Gemma4UnifiedProcessor`.
- Token mismatch: serving check validates the adapter tokenizer metadata and special tokens.
- ROCm OOM: keep `max_seq_length: 2048`, batch size `1`, gradient checkpointing on.
- NaN loss: training stops when configured to fail on non-finite loss.
- Missing evidence file: strict dataset mode fails with the exact row and path.
- vLLM adapter load failure: do not claim full serving support until the adapter loads with the
  deployed vLLM/transformers stack.
