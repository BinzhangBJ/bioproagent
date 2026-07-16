"""labclaude + OT-2 Deck Studio target adapter.

BioProAgent's public shell ships a vendor-agnostic ``nodes/consumables/
connections`` graph schema (``src/capabilities/verification/engine.py``,
``src/capabilities/automation/parameter_builder.py``) with the concrete
device registry, parameter-filling and rule-checking logic stripped out —
the README calls these out explicitly as "customization entry points".

This module is that customization for one concrete target: a lab whose
central controller is `labclaude <https://github.com/BinzhangBJ/labclaude>`_
and whose Opentrons OT-2 is driven through
`OT-2 Deck Studio <https://github.com/BinzhangBJ/ot2>`_ (which in turn acts
as labclaude's B-level Bridge Agent — see labclaude's
``docs/devices/B-sdk-dll/ot2.md``).

Design choice: rather than translate through the generic ``nodes/
consumables/connections`` graph (whose ``parameters`` field is
intentionally left schema-free in the public shell — see
``parameter_builder.py`` docstring — so a faithful generic→specific
translator can't be written without re-inventing exactly the private
mapping logic the shell removed), this target asks the LLM to emit our
two concrete, already-executable schemas directly:

    <exp_flow>
    {
      "ot2_protocol": {"name": str, "layout": {...}, "protocol": [...]},
      "labclaude_workflow": {"name": str, "steps": [...]}
    }
    </exp_flow>

``ot2_protocol`` matches OT-2 Deck Studio's named-protocol bundle (layout +
step list of pick_up_tip/aspirate/dispense/drop_tip/... ops — see that
repo's ``db.save_protocol`` / ``/api/v1/protocols``).
``labclaude_workflow`` matches labclaude's workflow IR (``Step`` dataclass
in ``backend/app/workflow/ir.py``: id/name/device_type/command/params/
depends_on/...). Its OT-2 step(s) must reference the OT-2 protocol by name:
``{"device_type": "ot2", "command": "run_protocol", "params": {"name": <ot2_protocol.name>}}``.

Wiring into the FSM: already done, gated behind an env var so the generic
public-shell path stays importable/runnable unchanged when it's off. See
``src/tools/tool_definitions.py``:

1. ``generate_machine_code`` — when ``LABCLAUDE_OT2_TARGET`` is truthy, uses
   ``build_paint_prompt_labclaude_ot2`` (this module, grounded live from
   labclaude's ``/api/device-types`` and OT-2's ``/api/v1/labware``) instead
   of ``build_paint_prompt``, and skips the ``parameter_builder.
   build_experiment_flow`` reshaping stage — our schema is already
   execution-ready, no vendor "parameter filling" needed.
2. ``validate_machine_code`` — same gate, calls
   ``validate_machine_code_labclaude_ot2`` instead of the generic
   ``validator_check``, translated back into the same
   ``{status, errors, rule_violations}`` dict shape the FSM's termination
   check (``main_evaluate.py::_should_terminate``) already understands, so
   no other FSM code needed to change.
3. Committing (``commit_machine_code_labclaude_ot2``) is deliberately
   **not** a tool in the FSM's tool graph — the public shell has no
   "deploy" step to model it on, and adding a brand-new tool means adding a
   new planner state/transition in ``planner_transfer_matrix.py``, untestable
   here without live LLM credentials. Instead it's called by the driving
   application after ``process_query`` returns with a passing
   ``kp_verification`` — see ``run_labclaude_agent.py`` (repo root) for a
   working, non-interactive example: reads a protocol file, runs one FSM
   session end to end, commits only if validation actually passed.

Configuration (``data/keys.env``, see ``keys.env.example``):
``LABCLAUDE_OT2_TARGET=1``, ``LABCLAUDE_BASE_URL``/``LABCLAUDE_TOKEN``,
``OT2_BASE_URL``/``OT2_TOKEN`` (an OT-2 Deck Studio API token from
Settings → API 设置 → 访问令牌), ``OT2_RUN_TIMEOUT_S``.

K_p ("physical/code checks") here isn't a static rule table (the public
shell's ``RuleEngine.check`` is a stub that always returns ``[]`` — see
``engine.py:107-115``) — it's the real thing: the OT-2 half is checked by
actually running the generated protocol to completion in OT-2 Deck
Studio's **simulation** backend (``ot2_engine.RunManager``), and the
labclaude half by labclaude's own IR compiler
(``backend/app/workflow/ir.py::compile_workflow``, exposed read-only via
``POST /api/workflows/validate``). Both catch real structural/physical
errors (unknown device_type/command, malformed steps, bad slot/well/labware
references) instead of a schema-shape check alone.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

# ---------------------------------------------------------------------
# Prompt (parallel to prompts.py::PAINT_PROMPT / build_paint_prompt)
# ---------------------------------------------------------------------

PAINT_PROMPT_LABCLAUDE_OT2 = """
Generate an executable experiment plan for a lab controlled by labclaude
(central scheduler) with an Opentrons OT-2 liquid handler bridged through
OT-2 Deck Studio.

Output JSON with exactly two top-level keys, enclosed by <exp_flow> ... </exp_flow>:

1. "ot2_protocol": {{"name": <short unique protocol name, kebab-case>,
   "layout": {{"slots": {{"<1-12>": {{"load_name": <opentrons labware load_name>}}, ...}},
               "pipettes": {{"left": <pipette load_name or "">, "right": <pipette load_name or "">}}}},
   "protocol": [{{"op": "pick_up_tip"|"aspirate"|"dispense"|"drop_tip", "mount": "left"|"right",
                  "slot": <int>, "well": <e.g. "A1">, "volume": <float, aspirate/dispense only>}}, ...]}}
   Only use ops from the list above. Every "slot" referenced in "protocol" must be defined in
   "layout.slots". Every step's "mount" must have a non-empty pipette in "layout.pipettes".

2. "labclaude_workflow": {{"name": <short unique workflow name>,
   "steps": [{{"id": <unique step id>, "name": <human label>, "device_type": <see catalog below>,
              "command": <a command listed for that device_type>, "params": {{...}},
              "depends_on": [<ids of steps that must finish first>]}}, ...]}}
   Exactly one step must drive the OT-2 protocol above:
   {{"device_type": "ot2", "command": "run_protocol", "params": {{"name": <ot2_protocol.name>}}}}.
   Steps for other equipment (centrifuge, plate reader, thermal cycler, ...) may be [MANUAL]
   (device_type "manual_station", command "operator_step", params.instruction = free text) if no
   automated device_type/command fits.

Available device_type -> command catalog (only use these; do not invent new ones):
{device_catalog}

Available OT-2 labware load_names (only use these in layout.slots[*].load_name):
{labware_catalog}

Aligned Protocol:
{protocol}

Experiment Info:
{exp_info}

Rectify Suggestion:
{suggestion}

Output only the JSON enclosed by <exp_flow> ... </exp_flow>. No prose outside the tags.
"""


def build_paint_prompt_labclaude_ot2(
    protocol: str,
    exp_info: str = "",
    suggestion: str = "[N/A]",
    device_catalog: str = "(catalog unavailable — see labclaude GET /api/device-types)",
    labware_catalog: str = "(catalog unavailable — see OT-2 Deck Studio GET /api/v1/labware)",
) -> str:
    return PAINT_PROMPT_LABCLAUDE_OT2.format(
        protocol=protocol or "None",
        exp_info=exp_info or "None",
        suggestion=suggestion or "[N/A]",
        device_catalog=device_catalog,
        labware_catalog=labware_catalog,
    )


# ---------------------------------------------------------------------
# Live schema grounding (labclaude's device registry + OT-2's labware
# catalog), so the prompt reflects what's *actually* registered right now
# rather than a stale hardcoded list — this is the "semantic grounding"
# BioProAgent's README claims (M_work-style symbol references) applied to
# our two concrete backends instead of their private registry.
# ---------------------------------------------------------------------

def fetch_device_catalog(labclaude_base_url: str, token: str, timeout: float = 5.0) -> str:
    r = requests.get(f"{labclaude_base_url.rstrip('/')}/api/device-types",
                     headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    types = r.json()
    lines = [f"- {t['device_type']}: {', '.join(t['commands'])}" for t in types]
    return "\n".join(lines)


def fetch_labware_catalog(ot2_base_url: str, token: str, timeout: float = 5.0) -> str:
    r = requests.get(f"{ot2_base_url.rstrip('/')}/api/v1/labware",
                     headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    items = r.json()["data"]["labware"]
    return "\n".join(f"- {i['load_name']} ({i['category']})" for i in items)


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

@dataclass
class LabclaudeConfig:
    base_url: str
    token: str


@dataclass
class OT2Config:
    base_url: str
    token: str
    run_poll_s: float = 0.3
    run_timeout_s: float = 120.0


@dataclass
class RuleViolation:
    rule_id: str
    severity: str   # "warn" | "halt" — mirrors verification.engine.Severity
    message: str


# ---------------------------------------------------------------------
# Validator (parallel to verification.engine.ProtocolValidator)
# ---------------------------------------------------------------------

_EXP_FLOW_RE = re.compile(r"<exp_flow>(.*?)</exp_flow>", re.DOTALL)


class LabclaudeOT2Validator:
    SCHEMA_KEYS = ("ot2_protocol", "labclaude_workflow")

    def extract_json(self, content: str) -> tuple[dict | None, str]:
        try:
            match = _EXP_FLOW_RE.search(content)
            payload = match.group(1).strip() if match else content.strip()
            if payload.startswith("```"):
                payload = re.sub(r"^```json?\s*", "", payload)
                payload = re.sub(r"\s*```$", "", payload)
            return json.loads(payload), ""
        except Exception as e:
            return None, f"JSONError: {e}"

    def check_shape(self, data: dict) -> list[str]:
        errors = []
        for key in self.SCHEMA_KEYS:
            if key not in data or not isinstance(data[key], dict):
                errors.append(f"SchemaError: missing or malformed '{key}'")
        if errors:
            return errors
        ot2 = data["ot2_protocol"]
        wf = data["labclaude_workflow"]
        if not ot2.get("name"):
            errors.append("SchemaError: ot2_protocol.name required")
        if not isinstance(ot2.get("protocol"), list):
            errors.append("SchemaError: ot2_protocol.protocol must be a list")
        if not wf.get("name"):
            errors.append("SchemaError: labclaude_workflow.name required")
        steps = wf.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("SchemaError: labclaude_workflow.steps must be a non-empty list")
            return errors
        ot2_steps = [s for s in steps if s.get("device_type") == "ot2"
                     and s.get("command") == "run_protocol"]
        if not ot2_steps:
            errors.append("SchemaError: labclaude_workflow needs one device_type=ot2/"
                          "command=run_protocol step")
        elif ot2_steps[0].get("params", {}).get("name") != ot2.get("name"):
            errors.append("LinkError: labclaude_workflow's run_protocol step params.name "
                          "must equal ot2_protocol.name")
        return errors

    def check_labclaude_workflow(self, wf: dict, cfg: LabclaudeConfig) -> list[str]:
        try:
            r = requests.post(f"{cfg.base_url.rstrip('/')}/api/workflows/validate",
                              headers={"Authorization": f"Bearer {cfg.token}"},
                              json={"name": wf["name"], "steps": wf["steps"]}, timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            return [f"LabclaudeConnectionError: {e}"]
        body = r.json()
        if not body.get("ok"):
            return [f"LabclaudeValidationError: {body.get('error')}"]
        return []

    _KNOWN_OPS = {"pick_up_tip", "aspirate", "dispense", "drop_tip", "delay", "home"}

    def check_ot2_protocol_structure(self, ot2: dict) -> list[str]:
        """Static structural check, run before touching the network.

        Found by direct testing, not assumption: OT-2 Deck Studio's
        simulation backend does NOT reject a step referencing a slot that
        was never declared in ``layout.slots`` — it's built for interactive
        UI animation, not strict validation of arbitrary externally
        generated protocols, so it just logs the action and reports
        ``status: done``. A generated protocol with a hallucinated slot
        number would otherwise sail through
        ``check_ot2_protocol_by_simulation`` undetected. This check closes
        that gap deterministically, without depending on ot2_engine
        internals.
        """
        errors: list[str] = []
        slots = set((ot2.get("layout") or {}).get("slots", {}).keys())
        pipettes = (ot2.get("layout") or {}).get("pipettes", {})
        for i, step in enumerate(ot2.get("protocol", [])):
            op = step.get("op")
            if op not in self._KNOWN_OPS:
                errors.append(f"OT2StructureError: step {i} unknown op {op!r}")
                continue
            mount = step.get("mount")
            if mount is not None and not pipettes.get(mount):
                errors.append(f"OT2StructureError: step {i} mount {mount!r} has no pipette "
                              "in layout.pipettes")
            slot = step.get("slot")
            if slot is not None and str(slot) not in slots:
                errors.append(f"OT2StructureError: step {i} references undeclared slot "
                              f"{slot!r} (declared: {sorted(slots)})")
        return errors

    def check_ot2_protocol_by_simulation(self, ot2: dict, cfg: OT2Config) -> list[str]:
        """K_p for the OT-2 half: actually run the generated protocol to
        completion against OT-2 Deck Studio's simulation backend. Catches
        unknown slots/wells/labware/pipette mismatches for real, instead of
        guessing from a static rule table."""
        base = cfg.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {cfg.token}"}
        try:
            r = requests.post(f"{base}/api/v1/connect", headers=headers,
                              json={"mode": "sim"}, timeout=cfg.run_timeout_s)
            r.raise_for_status()
            if not r.json().get("ok"):
                return [f"OT2ConnectError: {r.json().get('error')}"]

            r = requests.post(f"{base}/api/v1/protocol", headers=headers,
                              json={"protocol": ot2["protocol"], "layout": ot2.get("layout", {})},
                              timeout=10)
            r.raise_for_status()
            if not r.json().get("ok"):
                return [f"OT2LoadError: {r.json().get('error')}"]

            r = requests.post(f"{base}/api/v1/run/start", headers=headers, timeout=10)
            r.raise_for_status()
            if not r.json().get("ok"):
                return [f"OT2RunStartError: {r.json().get('error')}"]

            deadline = time.monotonic() + cfg.run_timeout_s
            while time.monotonic() < deadline:
                r = requests.get(f"{base}/api/v1/status", headers=headers, timeout=10)
                r.raise_for_status()
                snap = r.json()["data"]
                if snap["status"] == "done":
                    return []
                if snap["status"] == "error":
                    tail = [e["msg"] for e in snap.get("log", []) if e.get("level") == "error"]
                    return [f"OT2SimulationError: {tail[-1] if tail else 'run ended in error'}"]
                time.sleep(cfg.run_poll_s)
            return ["OT2SimulationError: timed out waiting for simulated run to finish"]
        except requests.RequestException as e:
            return [f"OT2ConnectionError: {e}"]


_validator = LabclaudeOT2Validator()


def validate_machine_code_labclaude_ot2(
    content: str, labclaude_cfg: LabclaudeConfig, ot2_cfg: OT2Config,
) -> tuple[bool, str, list[RuleViolation]]:
    """Drop-in replacement for verification.engine.validate_machine_code,
    same (ok, message, violations) contract for the existing Rectify loop."""
    data, err = _validator.extract_json(content)
    if err:
        return False, err, []

    shape_errors = _validator.check_shape(data)
    if shape_errors:
        return False, "; ".join(shape_errors), []

    structure_errors = _validator.check_ot2_protocol_structure(data["ot2_protocol"])
    if structure_errors:
        violations = [RuleViolation("K_p", "halt", m) for m in structure_errors]
        return False, "; ".join(structure_errors), violations

    errors = _validator.check_labclaude_workflow(data["labclaude_workflow"], labclaude_cfg)
    errors += _validator.check_ot2_protocol_by_simulation(data["ot2_protocol"], ot2_cfg)
    if errors:
        violations = [RuleViolation("K_p", "halt", m) for m in errors]
        return False, "; ".join(errors), violations
    return True, "Validation passed (labclaude+OT-2 target).", []


# ---------------------------------------------------------------------
# Commit — only called after validate_machine_code_labclaude_ot2 passes.
# The public shell has no equivalent (it never deploys anything); this is
# what actually makes the generated plan runnable.
# ---------------------------------------------------------------------

def commit_machine_code_labclaude_ot2(
    content: str, labclaude_cfg: LabclaudeConfig, ot2_cfg: OT2Config,
) -> dict[str, Any]:
    data, err = _validator.extract_json(content)
    if err:
        raise ValueError(err)
    ot2, wf = data["ot2_protocol"], data["labclaude_workflow"]

    r = requests.post(f"{ot2_cfg.base_url.rstrip('/')}/api/v1/protocols",
                      headers={"Authorization": f"Bearer {ot2_cfg.token}"},
                      json={"name": ot2["name"], "protocol": ot2["protocol"],
                            "layout": ot2.get("layout", {})}, timeout=10)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(f"failed to save OT-2 protocol: {r.json().get('error')}")

    r = requests.post(f"{labclaude_cfg.base_url.rstrip('/')}/api/workflows",
                      headers={"Authorization": f"Bearer {labclaude_cfg.token}"},
                      json={"name": wf["name"], "steps": wf["steps"]}, timeout=10)
    r.raise_for_status()
    workflow = r.json()

    return {"ot2_protocol_name": ot2["name"], "labclaude_workflow": workflow}
