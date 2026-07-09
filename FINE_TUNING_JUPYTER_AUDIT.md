# Fine-Tuning / Jupyter Notebook Audit

Date: 2026-07-10
Workspace: `C:\Users\Admin\OneDrive\Documents\New folder\amd act II`

## Audit Scope

- Local working tree on `feature/chat-first-approval-ui`.
- GitHub remotes fetched with `git fetch --all --prune`.
- Remote branches compared: `origin/main`, `origin/feature/chat-first-approval-ui`,
  `origin/gpu-notebook-testing`.
- Local `.claude/worktrees/great-spence-fd71bc` worktree inspected because it tracks
  `gpu-notebook-testing` and contains notebook/source files missing from the main worktree.
- Attached terminal/Jupyter evidence inspected from
  `C:\Users\Admin\.codex\attachments\dc0cb54c-c0e4-4064-9a18-597ee55c42dc\pasted-text.txt`.
- Local adapter metadata inspected at `shelfwise-gemma-final-adapter/final_adapter`.

## Local And GitHub Findings

1. Current branch is `feature/chat-first-approval-ui` at `c14c297`, matching
   `origin/feature/chat-first-approval-ui`.
2. The main worktree is already dirty before this audit. Existing modified files include frontend,
   inference, backend, tests, and README changes. I did not revert them.
3. `origin/gpu-notebook-testing` is a separate richer branch at `7160cef`. It contains the actual
   notebook set, MLOps, synthdata, multimodal, connector, storage, resilience, and broader test
   packages. The current branch has several matching package directories with only `__pycache__`,
   which is a merge-risk signal.
4. The current main worktree has no tracked top-level notebook source except a checkpoint under
   `notebooks/.ipynb_checkpoints`. The real notebooks are in the `.claude` worktree:
   `00_shelfwise_llm_training_bootstrap.ipynb`, `01_shelfwise_full_test_harness.ipynb`,
   `02_shelfwise_gemma_finetune.ipynb`, and `03_shelfwise_stress_test.ipynb`.
5. Local adapter metadata is real and important: `adapter_config.json` points to
   `google/gemma-4-12B-it`, PEFT LoRA, `r=16`, `lora_alpha=32`, `lora_dropout=0.05`, and target
   modules include `patch_dense`, `embedding_projection`, `q_proj`, `k_proj`, `v_proj`, `o_proj`,
   `gate_proj`, `up_proj`, and `down_proj`.
6. Local tokenizer metadata says `processor_class: Gemma4UnifiedProcessor`, `tokenizer_class:
   GemmaTokenizer`, left padding, placeholder `model_max_length`, and the required multimodal/tool
   tokens are present.
7. The attached notebook evidence shows the GPU host was `AMD Radeon Pro W7900D`, `gfx1100`,
   48 GB VRAM, ROCm/HIP 7.2, not a confirmed MI300X / `gfx942` box.
8. The attached `groups: cannot find name for group ID 109` line is container/user mapping noise
   unless another permission failure appears.
9. `curl http://127.0.0.1:8000/v1/models` first failed because no vLLM server was listening.
10. The adapter was saved and archived: `/workspace/checkpoints/shelfwise-gemma/final_adapter`
    and `/workspace/shelfwise-gemma-final-adapter.tar.gz` at 237 MB.
11. vLLM found `/opt/venv/bin/vllm` version `0.16.1.dev0+g89a77b108.d20260318.rocm721`.
12. vLLM serving failed because the runtime Transformers stack did not recognize model type
    `gemma4_unified`. The next fix is runtime compatibility, not redoing the adapter from scratch.

## Risk Analysis

- Highest risk: notebook-only training is not reproducible enough for tomorrow's 8-hour run. A
  scriptable preflight/train/eval/serving path is needed.
- High risk: current branch and `gpu-notebook-testing` branch are split. Copying everything blindly
  would collide with the chat-first UI work; ignoring it would lose the broader backend/MLOps work.
- High risk: vLLM adapter serving is not proven for `gemma4_unified` until the ROCm host upgrades or
  pins a compatible Transformers/vLLM stack.
- Medium risk: raw audio/video tensors are not proven. The safe claim today is transcript and sampled
  frame fallback, not full native audio/video training.
- Medium risk: tokenizer placeholder max length can silently create unsafe sequence lengths unless
  the harness requires explicit `max_seq_length`.
- Medium risk: missing `patch_dense` or `embedding_projection` would silently downgrade the project
  to text-only LoRA unless preflight fails loudly.

## Implementation Added In This Audit

- Added `configs/train_gemma4_multimodal.yaml`.
- Added canonical multimodal JSONL fixtures under `data/training/` and `data/eval/`.
- Added local evidence fixtures under `data/evidence/smoke/`.
- Added `src/shelfwise/training/` with:
  - config loading and safety validation,
  - dataset schema validation,
  - evidence-aware prompt/collator helpers,
  - strict preflight command,
  - smoke/8-hour train command,
  - eval command producing JSONL and Markdown,
  - adapter/tokenizer serving compatibility check.
- Added `shelfwise.training.simulation` to generate canonical multimodal episodes from a ShelfWise
  world-simulation model instead of relying only on static rows.
- Added `shelfwise.training.shakedown` as the full gated application AI run:
  `preflight -> simulation dataset build -> smoke train -> full train -> eval -> serving check -> final report`.
- Added Jupyter server bootstrap/check scripts under `scripts/`.
- Added `tests/test_gemma4_training_harness.py`.
- Added `tests/test_shakedown_pipeline.py`.
- Added README section `Gemma 4 Multimodal Training Harness`.
- Added README section `Gemma 4 Multimodal Full Shakedown`.
- Added `runs/` to `.gitignore` so training/eval outputs do not become accidental repo churn.

## Commands Added

```powershell
$env:PYTHONPATH="src"
python -m shelfwise.training.preflight --config configs/train_gemma4_multimodal.yaml
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --max_steps 20 --run_name smoke-mm
python -m shelfwise.training.train --config configs/train_gemma4_multimodal.yaml --run_name gemma4-mm-8h-001
python -m shelfwise.training.evaluate --config configs/train_gemma4_multimodal.yaml --dry-run
python -m shelfwise.training.serving_check --config configs/train_gemma4_multimodal.yaml --adapter-path shelfwise-gemma-final-adapter/final_adapter --skip-model-load
python -m shelfwise.training.shakedown --config configs/train_gemma4_multimodal.yaml --run_name shelfwise-mm-full-8h-001
bash scripts/jupyter_gemma4_bootstrap.sh
```

## Verification Run Locally

- `python -m pytest tests/test_gemma4_training_harness.py -q` passed: 5 tests.
- `python -m pytest tests/test_training_data.py tests/test_inference_readiness.py tests/test_eval_harness.py -q`
  passed: 8 tests.
- Combined focused run passed: 13 tests.
- Shakedown focused run passed: `tests/test_gemma4_training_harness.py tests/test_shakedown_pipeline.py`
  with 7 tests.
- `python -m ruff check src/shelfwise/training tests/test_gemma4_training_harness.py` passed.
- `python -m ruff check src/shelfwise/training tests/test_gemma4_training_harness.py tests/test_shakedown_pipeline.py`
  passed.
- Dry-run eval command passed with `PYTHONPATH=src` and wrote ignored output under `runs/`.
- Dry-run shakedown command passed and wrote simulation datasets, eval artifacts, and
  `shakedown_report.md` under `runs/`.
- `python -m pip install -e .` passed, and the shakedown dry run works without `PYTHONPATH` after
  editable install.
- Adapter metadata serving check passed with `--skip-model-load`; it confirmed Gemma 4 base model,
  `Gemma4UnifiedProcessor`, `GemmaTokenizer`, and required target modules in the exported adapter.

## Remaining Blockers

1. Heavy preflight was not run locally because this Windows environment is not the ROCm GPU notebook
   host and would not prove Gemma 4 loading.
2. Smoke training was not run locally for the same reason.
3. The ROCm host still needs the `gemma4_unified` serving compatibility fix: upgrade/pin
   Transformers and vLLM until both the base model and LoRA adapter load.
4. The branch split between `feature/chat-first-approval-ui` and `gpu-notebook-testing` still needs a
   deliberate merge plan. Do not blindly copy the whole `.claude` worktree into this branch.
5. Full native audio/video training is not yet proven. Current harness preserves multimodal tokens
   and uses honest transcript/frame fallbacks until processor-level raw media support is confirmed.
