from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Gemma4Profile:
    """Stable identity for one supported Gemma 4 base-model size."""

    name: str
    model_name_or_path: str
    size: str
    default_revision: str

    @property
    def adapter_namespace(self) -> str:
        return f"{self.name}@{self.default_revision}"


GEMMA4_PROFILES: dict[str, Gemma4Profile] = {
    "gemma-4-e2b-it": Gemma4Profile(
        name="gemma-4-e2b-it",
        model_name_or_path="google/gemma-4-E2B-it",
        size="E2B",
        default_revision="3e22461f65e89153144f8adb70e3b8c2cc9845a7",
    ),
    "gemma-4-e4b-it": Gemma4Profile(
        name="gemma-4-e4b-it",
        model_name_or_path="google/gemma-4-E4B-it",
        size="E4B",
        default_revision="ee0ef6023621cff504d758262d4e04895a5af4a2",
    ),
    "gemma-4-12b-it": Gemma4Profile(
        name="gemma-4-12b-it",
        model_name_or_path="google/gemma-4-12B-it",
        size="12B",
        default_revision="707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7",
    ),
    "gemma-4-31b-it": Gemma4Profile(
        name="gemma-4-31b-it",
        model_name_or_path="google/gemma-4-31B-it",
        size="31B",
        default_revision="842da3794eaa0b77d5f08bae87a17459d91ff475",
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
