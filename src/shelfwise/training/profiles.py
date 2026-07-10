from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Gemma4Profile:
    """Stable identity for one supported Gemma 4 base-model size."""

    name: str
    model_name_or_path: str
    size: str
    default_revision: str = "main"

    @property
    def adapter_namespace(self) -> str:
        return f"{self.name}@{self.default_revision}"


GEMMA4_PROFILES: dict[str, Gemma4Profile] = {
    "gemma-4-e2b-it": Gemma4Profile(
        name="gemma-4-e2b-it",
        model_name_or_path="google/gemma-4-E2B-it",
        size="E2B",
    ),
    "gemma-4-e4b-it": Gemma4Profile(
        name="gemma-4-e4b-it",
        model_name_or_path="google/gemma-4-E4B-it",
        size="E4B",
    ),
    "gemma-4-12b-it": Gemma4Profile(
        name="gemma-4-12b-it",
        model_name_or_path="google/gemma-4-12B-it",
        size="12B",
    ),
    "gemma-4-31b-it": Gemma4Profile(
        name="gemma-4-31b-it",
        model_name_or_path="google/gemma-4-31B-it",
        size="31B",
    ),
}

PROFILE_BY_MODEL_ID = {
    profile.model_name_or_path: profile for profile in GEMMA4_PROFILES.values()
}


def get_gemma4_profile(name: str) -> Gemma4Profile:
    try:
        return GEMMA4_PROFILES[name]
    except KeyError as exc:
        supported = ", ".join(sorted(GEMMA4_PROFILES))
        message = f"unsupported Gemma 4 profile {name!r}; choose one of: {supported}"
        raise ValueError(message) from exc


def profile_for_model(model_name_or_path: str) -> Gemma4Profile:
    try:
        return PROFILE_BY_MODEL_ID[model_name_or_path]
    except KeyError as exc:
        supported = ", ".join(sorted(PROFILE_BY_MODEL_ID))
        raise ValueError(
            f"unsupported Gemma 4 model {model_name_or_path!r}; choose one of: {supported}"
        ) from exc
