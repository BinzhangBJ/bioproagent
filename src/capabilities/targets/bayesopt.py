"""BayesOpt Agent target adapter: a thin HTTP client for the standalone
`bayesopt-agent <https://github.com/BinzhangBJ/bayesopt-agent>`_ service — a
from-scratch Gaussian-process/Expected-Improvement advisor for
**experiment/protocol parameters** (temperature, cycle count, buffer choice,
...), not scheduler/device runtime parameters like labclaude's own
optimization roadmap (see labclaude's docs/optimization-roadmap.md — a
different, unrelated kind of "optimization").

Design choice, mirroring this package's ``labclaude_ot2.py`` and its
``commit_machine_code_labclaude_ot2`` precedent (see that module's docstring
for the full reasoning): this is deliberately **not** a new FSM ``@tool``.
Adding one means a new planner state/transition in
``planner_transfer_matrix.py``, which can't be meaningfully exercised without
live LLM credentials — same reason the labclaude/OT-2 "commit" step lives
outside the tool graph. Instead, BayesOpt is a **driving-script** step around
the existing Design->Verify->Rectify loop, wired into ``run_labclaude_agent.py``
via ``--bayesopt-study``:

1. **Before** starting an FSM session: call ``suggest(study)`` to get the next
   parameter suggestion, fold it into the ``exp_info`` text handed to the FSM.
   The FSM runs its normal loop unaware that "temperature=37.2" came from a
   Gaussian process instead of a human typing it in.
2. **After** a real experiment finishes and its outcome is known — which can
   only happen once the wet-lab run completes, a separate point in time from
   step 1, usually a separate invocation of the driving script entirely —
   call ``observe(study, params, objective)`` to feed the result back so the
   next ``suggest`` call is actually informed by it.

Configuration (``data/keys.env``, see ``keys.env.example``):
``BAYESOPT_TARGET=1``, ``BAYESOPT_BASE_URL``, ``BAYESOPT_TOKEN`` (minted by
the bayesopt-agent service's ``scripts/create_token.py`` — that service has
no user/login system, just Bearer tokens, since it's a stateless advisor,
not a system of record).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class BayesOptConfig:
    base_url: str
    token: str
    timeout: float = 10.0


def create_study_if_absent(
    cfg: BayesOptConfig, name: str, direction: str, param_space: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the study on first use; a 409 (already exists) is expected and
    fine here — a study is meant to persist across many experiment rounds,
    this call just makes the driving script idempotent so it doesn't need a
    separate one-time setup step."""
    r = requests.post(
        f"{cfg.base_url.rstrip('/')}/api/v1/studies",
        headers={"Authorization": f"Bearer {cfg.token}"},
        json={"name": name, "direction": direction, "param_space": param_space},
        timeout=cfg.timeout,
    )
    if r.status_code == 409:
        return get_study(cfg, name)
    r.raise_for_status()
    return r.json()


def get_study(cfg: BayesOptConfig, name: str) -> dict[str, Any]:
    r = requests.get(f"{cfg.base_url.rstrip('/')}/api/v1/studies/{name}",
                     headers={"Authorization": f"Bearer {cfg.token}"}, timeout=cfg.timeout)
    r.raise_for_status()
    return r.json()


def suggest(cfg: BayesOptConfig, study_name: str) -> dict[str, Any]:
    """Returns the next parameter dict to try, e.g.
    ``{"temperature": 37.2, "cycles": 5, "buffer": "PBS"}``."""
    r = requests.post(
        f"{cfg.base_url.rstrip('/')}/api/v1/studies/{study_name}/suggest",
        headers={"Authorization": f"Bearer {cfg.token}"}, timeout=cfg.timeout,
    )
    r.raise_for_status()
    return r.json()["params"]


def observe(cfg: BayesOptConfig, study_name: str, params: dict[str, Any],
           objective: float) -> dict[str, Any]:
    """Feed back a completed round's real-world outcome (measured after the
    physical experiment finishes — the whole reason this is a separate call
    from ``suggest``, not a single round-trip)."""
    r = requests.post(
        f"{cfg.base_url.rstrip('/')}/api/v1/studies/{study_name}/observe",
        headers={"Authorization": f"Bearer {cfg.token}"},
        json={"params": params, "objective": objective}, timeout=cfg.timeout,
    )
    r.raise_for_status()
    return r.json()


def format_suggestion_for_prompt(params: dict[str, Any]) -> str:
    """Render a suggested parameter dict as short plain text to fold into
    ``exp_info``, e.g. ``"temperature=37.2, cycles=5, buffer=PBS"`` — plain
    enough for the LLM to read as ordinary experiment context alongside the
    protocol text, no special parsing on its end required."""
    return ", ".join(f"{k}={v}" for k, v in params.items())
