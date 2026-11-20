"""Pure recovery policy; importing this module never starts ANSYS."""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any


DEFAULT_RETRY = {
    "enabled": True,
    "max_attempts": 3,
    "max_startup_attempts": 2,
    "backoff_seconds": 5.0,
    "backoff_multiplier": 2.0,
    "max_backoff_seconds": 30.0,
    # Absolute, model-specific sizes, in order. No universal mesh-size fallback.
    "mesh_sizes_mm": [],
    "solver_substeps": [],
}


def positive_number(value: Any, name: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("%s must be a finite number" % name)
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("%s must be a finite number" % name) from exc
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError("%s must be %s and finite" % (name, "nonnegative" if allow_zero else "positive"))
    return number


def positive_integer(value: Any, name: str, limit: int | None = None) -> int:
    number = positive_number(value, name)
    if number != int(number) or (limit is not None and number > limit):
        raise ValueError("%s must be an integer%s" % (name, " <= %d" % limit if limit else ""))
    return int(number)


def validate_substeps(profile: Any, name: str) -> dict[str, int]:
    if not isinstance(profile, dict) or set(profile) != {"initial", "minimum", "maximum"}:
        raise ValueError("%s requires exactly initial, minimum and maximum" % name)
    values = {key: positive_integer(profile[key], name + "." + key) for key in profile}
    if not values["minimum"] <= values["initial"] <= values["maximum"]:
        raise ValueError("%s requires minimum <= initial <= maximum" % name)
    return values


def retry_settings(config: dict[str, Any]) -> dict[str, Any]:
    supplied = config.get("retry", {})
    if not isinstance(supplied, dict):
        raise ValueError("retry must be an object")
    unknown = set(supplied) - set(DEFAULT_RETRY)
    if unknown:
        raise ValueError("Unknown retry keys: " + ", ".join(sorted(unknown)))
    settings = deepcopy(DEFAULT_RETRY)
    settings.update(deepcopy(supplied))
    if not isinstance(settings["enabled"], bool):
        raise ValueError("retry.enabled must be true or false")
    for key in ("max_attempts", "max_startup_attempts"):
        settings[key] = positive_integer(settings[key], "retry." + key, limit=10)
    for key in ("backoff_seconds", "max_backoff_seconds"):
        settings[key] = positive_number(settings[key], "retry." + key, allow_zero=True)
    settings["backoff_multiplier"] = positive_number(settings["backoff_multiplier"], "retry.backoff_multiplier")
    if settings["backoff_multiplier"] < 1:
        raise ValueError("retry.backoff_multiplier must be >= 1")
    for key in ("mesh_sizes_mm", "solver_substeps"):
        if not isinstance(settings[key], list) or len(settings[key]) > 9:
            raise ValueError("retry.%s must be a list with at most 9 entries" % key)
    sizes = [positive_number(value, "retry.mesh_sizes_mm") for value in settings["mesh_sizes_mm"]]
    if len(set(sizes)) != len(sizes):
        raise ValueError("retry.mesh_sizes_mm must not contain duplicates")
    base_size = config.get("analysis", {}).get("mesh_size_mm")
    if config.get("workflow") != "inspect" or base_size is not None:
        base_size = positive_number(base_size, "analysis.mesh_size_mm")
    if base_size in sizes:
        raise ValueError("retry.mesh_sizes_mm must differ from the initial mesh size")
    settings["mesh_sizes_mm"] = sizes
    settings["solver_substeps"] = [
        validate_substeps(profile, "retry.solver_substeps[%d]" % index)
        for index, profile in enumerate(settings["solver_substeps"])
    ]
    initial_profile = config.get("analysis", {}).get("substeps")
    if initial_profile is not None:
        validate_substeps(initial_profile, "analysis.substeps")
    return settings


def classify_failure(diagnostic: dict[str, Any]) -> str:
    """Only explicit, stage-appropriate failures enter the retry allowlist."""
    stage = diagnostic.get("stage", "unknown")
    if stage in {"user_input", "geometry", "material", "boundary", "solver_settings", "results", "delivery", "save"}:
        return stage + "_error"
    if diagnostic.get("exception_type") in {"AttributeError", "TypeError", "ImportError", "NameError", "SyntaxError"}:
        return "api_error"
    evidence = "\n".join(str(diagnostic.get(key, "")) for key in ("error", "messages", "solver_logs")).lower()
    # These take priority over recoverable errors that might also be in a log.
    for phrases, code in (
        (("out of memory", "insufficient memory", "not enough memory"), "resource_error"),
        (("rigid body motion", "unconstrained", "singular matrix", "negative pivot", "zero pivot", "small pivot"), "constraint_error"),
        (("invalid geometry", "geometry is invalid", "invalid material"), "model_error"),
    ):
        if any(phrase in evidence for phrase in phrases):
            return code
    if stage in {"create", "startup", "mesh", "solve"} and any(phrase in evidence for phrase in (
        "license checkout failed", "license check out failed", "not enough licenses",
        "licensed number of users", "unable to connect to license server",
        "license server is unavailable", "all licenses are in use",
    )):
        return "license_unavailable"
    if stage == "solve" and any(phrase in evidence for phrase in (
        "failed to converge", "did not converge", "not converged", "nonconvergence", "convergence failure",
    )):
        return "nonconvergence"
    if stage == "mesh" and diagnostic.get("kind") == "mesh_failed":
        return "mesh_failed"
    return "unknown_error"


def recovery_action(settings: dict[str, Any], code: str, attempt: int,
                    mesh_index: int, solver_index: int) -> dict[str, Any]:
    if not settings["enabled"] or attempt >= settings["max_attempts"]:
        return {"action": "stop", "reason": "disabled_or_attempt_limit"}
    if code == "license_unavailable":
        return {"action": "retry_same_settings"}
    if code == "mesh_failed" and mesh_index < len(settings["mesh_sizes_mm"]):
        return {"action": "change_mesh", "mesh_size_mm": settings["mesh_sizes_mm"][mesh_index]}
    if code == "nonconvergence" and solver_index < len(settings["solver_substeps"]):
        return {"action": "change_substeps", "substeps": deepcopy(settings["solver_substeps"][solver_index])}
    return {"action": "stop", "reason": "no_approved_recovery"}


def backoff_seconds(settings: dict[str, Any], retry_index: int) -> float:
    return min(settings["max_backoff_seconds"],
               settings["backoff_seconds"] * settings["backoff_multiplier"] ** max(0, retry_index - 1))
