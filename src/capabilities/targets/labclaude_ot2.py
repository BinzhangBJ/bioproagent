"""labclaude target adapter.

BioProAgent's public shell ships a vendor-agnostic ``nodes/consumables/
connections`` graph schema (``src/capabilities/verification/engine.py``,
``src/capabilities/automation/parameter_builder.py``) with the concrete
device registry, parameter-filling and rule-checking logic stripped out —
the README calls these out explicitly as "customization entry points".

This module is that customization for one concrete target: a lab whose
central controller is `labclaude <https://github.com/BinzhangBJ/labclaude>`_.
labclaude owns a whole device registry (liquid handlers, plate readers,
thermal cyclers, robot arms, an Opentrons OT-2 among others — see
``GET /api/device-types``); this target treats all of them uniformly through
that registry instead of hardcoding OT-2 as a special case.

Design choice: rather than translate through the generic ``nodes/
consumables/connections`` graph (whose ``parameters`` field is
intentionally left schema-free in the public shell — see
``parameter_builder.py`` docstring — so a faithful generic→specific
translator can't be written without re-inventing exactly the private
mapping logic the shell removed), this target asks the LLM to emit
labclaude's own workflow IR directly:

    <exp_flow>
    {"name": str, "steps": [{"id": str, "name": str, "device_type": str,
                              "command": str, "params": {...},
                              "depends_on": [str, ...]}, ...]}
    </exp_flow>

This matches labclaude's workflow IR exactly (``Step`` dataclass in
``backend/app/workflow/ir.py``: id/name/device_type/command/params/
depends_on/...) — what the LLM emits is what gets POSTed to
``POST /api/workflows``, no reshaping in between.

OT-2 is deliberately *not* special-cased here. labclaude's own compiler
(``backend/app/workflow/ot2_compile.py::fuse_ot2_steps``) lets OT-2
pipetting be written as an ordinary sequence of steps
(``device_type="ot2"``, ``command`` one of ``pick_up_tip``/``aspirate``/
``dispense``/``mix``/``blow_out``/``drop_tip``/``return_tip``/``move``/
``home``) with normal ``depends_on`` chaining, same as any other device —
labclaude auto-fuses a strictly-chained run of these into one protocol
dispatch at compile time and fills in the deck layout itself from live
labware-tracker state at run time. So the only OT-2-specific knowledge
this module needs is: what params each of those commands takes, and that
fusion wants strict sequential chaining (see ``PAINT_PROMPT`` and
``_check_ot2_step_shape``/``_check_ot2_fusion_chaining`` below) — everything
else (which device_types/commands exist at all) comes from labclaude's own
``GET /api/device-types``, live, same as for every other device.

An earlier version of this module asked the LLM for a *second*, separate
schema (an ``ot2_protocol`` object with its own deck layout + step list,
stored via OT-2 Deck Studio's ``/api/v1/protocols`` and referenced from a
single ``run_protocol`` labclaude step) and validated the OT-2 half by
actually running it against OT-2 Deck Studio's simulation backend over
HTTP. That predates ``fuse_ot2_steps`` — labclaude no longer has a
``run_protocol`` command at all, so machine code in that shape fails
labclaude's compiler outright. It has been removed rather than kept
side-by-side: the design it served no longer exists on the labclaude side,
and the single-schema version below is strictly less work for the LLM to
get right (no cross-referencing two documents, no deck layout to author).

Wiring into the FSM: already done, gated behind an env var so the generic
public-shell path stays importable/runnable unchanged when it's off. See
``src/tools/tool_definitions.py``:

1. ``generate_machine_code`` — when ``LABCLAUDE_TARGET`` is truthy, uses
   ``build_paint_prompt_labclaude`` (this module, grounded live from
   labclaude's ``/api/device-types``) instead of ``build_paint_prompt``, and
   skips the ``parameter_builder.build_experiment_flow`` reshaping stage —
   our schema is already execution-ready, no vendor "parameter filling"
   needed.
2. ``validate_machine_code`` — same gate, calls
   ``validate_machine_code_labclaude`` instead of the generic
   ``validator_check``, translated back into the same
   ``{status, errors, rule_violations}`` dict shape the FSM's termination
   check (``main_evaluate.py::_should_terminate``) already understands, so
   no other FSM code needed to change.
3. Committing (``commit_machine_code_labclaude``) is deliberately **not** a
   tool in the FSM's tool graph — the public shell has no "deploy" step to
   model it on, and adding a brand-new tool means adding a new planner
   state/transition in ``planner_transfer_matrix.py``, untestable here
   without live LLM credentials. Instead it's called by the driving
   application after ``process_query`` returns with a passing
   ``kp_verification`` *and after a human has reviewed the generated
   workflow* — see ``run_labclaude_agent.py`` (repo root): it prints a
   human-readable rendering of every step and requires explicit
   confirmation before committing anything. Nothing in this module commits
   without that caller-side gate; it isn't re-implemented here because it's
   a driving-application concern (what "review" looks like — TTY prompt,
   web approval queue, Slack message — is a UI decision, not a validation
   one).

K_p ("physical/code checks") here has two layers:

1. **Structural** (``check_workflow_structure``, fully local/offline — no
   network calls, only needs the device catalog already fetched for the
   prompt): mirrors labclaude's own IR compiler rules (step id uniqueness,
   dependency references resolve and don't cycle, every
   ``device_type``/``command`` pair is actually registered) plus
   OT-2-specific parameter shape checks (mount is "left"/"right", slot is
   1-12, volume is a positive number where required) and a *fusion-chaining*
   check specific to labclaude's compiler behavior: consecutive OT-2
   pipetting steps that don't strictly chain (``depends_on == [previous
   step's id]``) still compile and run correctly, just as separate
   protocol dispatches instead of one batched run — that's a ``warn``, not
   a ``halt``, since it's a performance/intent smell rather than a
   correctness bug.

   This exists specifically because ``POST /api/workflows/validate`` (a
   dry-run-without-persisting endpoint that used to exist on the labclaude
   side and that an earlier version of this module depended on) is
   currently gone from labclaude's API — apparently lost in an unrelated
   rebase, and it was never covered by a test on either side, so its
   absence went unnoticed. Rather than ask for it to be restored,
   ``check_workflow_structure`` below reimplements the same checks
   client-side against the live device catalog, so this target has no
   dependency on labclaude beyond what already exists and is stable:
   ``GET /api/device-types`` (read) and ``POST /api/workflows`` (the
   commit step, which also — as a defense-in-depth safety net, not the
   primary check — re-validates server-side via labclaude's own
   ``compile_workflow`` before persisting anything).

2. **Authoritative** (at commit time only): the real ``POST /api/workflows``
   call. labclaude's ``compile_workflow`` validates before persisting
   (invalid input raises before any DB write), so a structurally-unsound
   workflow that somehow slipped past layer 1 still can't get persisted —
   it just fails later, at commit, instead of during Design→Verify→Rectify.

Configuration (``data/keys.env``, see ``keys.env.example``):
``LABCLAUDE_TARGET=1``, ``LABCLAUDE_BASE_URL``/``LABCLAUDE_TOKEN``. No OT-2
Deck Studio URL/token is needed for this module any more — OT-2 is reached
exclusively through labclaude's own Bridge Agent at run time, which this
module (a design-time tool) never talks to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

# ---------------------------------------------------------------------
# Prompt (parallel to prompts.py::PAINT_PROMPT / build_paint_prompt)
# ---------------------------------------------------------------------

PAINT_PROMPT_LABCLAUDE = """
Generate an executable experiment plan for a lab controlled by labclaude
(central scheduler, multiple registered device types including an
Opentrons OT-2 liquid handler).

Output JSON enclosed by <exp_flow> ... </exp_flow>, matching labclaude's
workflow IR exactly:

{{"name": <short unique workflow name>,
  "steps": [{{"id": <unique step id>, "name": <human label>,
             "device_type": <see catalog below>,
             "command": <a command listed for that device_type>,
             "params": {{...}},
             "depends_on": [<ids of steps that must finish first>]}}, ...]}}

Steps for equipment with no automated device_type/command that fits may use
device_type "manual_station", command "operator_step",
params.instruction = free text (a human performs that step).

Available device_type -> command catalog (only use these; do not invent new
ones):
{device_catalog}

--- OT-2 liquid handling (device_type "ot2") ---
Write pipetting as an ordinary sequence of "ot2" steps, exactly like any
other device — do NOT invent a separate protocol object or a "run_protocol"
command (it does not exist). Do NOT author a deck layout; labclaude fills it
in itself at run time from what's actually loaded on the machine.

Param shape per command:
  pick_up_tip {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">}}
  drop_tip    {{"mount": "left"|"right"}}
  return_tip  {{"mount": "left"|"right"}}
  aspirate    {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">,
               "volume": <µL, > 0>}}
  dispense    {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">,
               "volume": <µL, > 0>}}
  mix         {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">,
               "volume": <µL, > 0>, "mix_reps": <int>}}
  blow_out    {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">}}
  move        {{"mount": "left"|"right", "slot": <1-12>, "well": <e.g. "A1">}}
  home        {{}}

IMPORTANT: consecutive "ot2" pipetting steps only get batched into one
physical protocol run if each one's "depends_on" is EXACTLY a single-item
list containing the immediately preceding "ot2" step's id — a strict chain,
one step to the next, in the order they should execute. If you branch,
merge, or interleave other devices' steps in between, each broken link
starts a new separate OT-2 run instead (still correct, just less
efficient) — prefer one unbroken chain per contiguous block of OT-2
pipetting whenever the protocol allows it.

Aligned Protocol:
{protocol}

Experiment Info:
{exp_info}

Rectify Suggestion:
{suggestion}

Output only the JSON enclosed by <exp_flow> ... </exp_flow>. No prose outside the tags.
"""


def build_paint_prompt_labclaude(
    protocol: str,
    exp_info: str = "",
    suggestion: str = "[N/A]",
    device_catalog: str = "(catalog unavailable — see labclaude GET /api/device-types)",
) -> str:
    return PAINT_PROMPT_LABCLAUDE.format(
        protocol=protocol or "None",
        exp_info=exp_info or "None",
        suggestion=suggestion or "[N/A]",
        device_catalog=device_catalog,
    )


# ---------------------------------------------------------------------
# Live schema grounding (labclaude's device registry), so the prompt and
# the structural checker reflect what's *actually* registered right now
# rather than a stale hardcoded list — this is the "semantic grounding"
# BioProAgent's README claims (M_work-style symbol references) applied to
# labclaude's device registry instead of their private one.
# ---------------------------------------------------------------------

def fetch_device_catalog(labclaude_base_url: str, token: str, timeout: float = 5.0) -> str:
    """Human/LLM-readable catalog text, for the prompt."""
    types = _get_device_types(labclaude_base_url, token, timeout)
    lines = [f"- {t['device_type']}: {', '.join(t['commands'])}" for t in types]
    return "\n".join(lines)


def fetch_device_catalog_map(
    labclaude_base_url: str, token: str, timeout: float = 5.0,
) -> dict[str, set[str]]:
    """Same catalog as ``fetch_device_catalog`` but as ``{device_type:
    {command, ...}}``, for structural validation (membership checks) rather
    than prompt text. Kept as a separate call (not derived from the string
    above) so each has one obvious purpose."""
    types = _get_device_types(labclaude_base_url, token, timeout)
    return {t["device_type"]: set(t["commands"]) for t in types}


def _get_device_types(labclaude_base_url: str, token: str, timeout: float) -> list[dict]:
    r = requests.get(f"{labclaude_base_url.rstrip('/')}/api/device-types",
                     headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

@dataclass
class LabclaudeConfig:
    base_url: str
    token: str


@dataclass
class RuleViolation:
    rule_id: str
    severity: str   # "warn" | "halt" — mirrors verification.engine.Severity
    message: str


# ---------------------------------------------------------------------
# Structural validator (parallel to verification.engine.ProtocolValidator,
# and to labclaude's own backend/app/workflow/ir.py::compile_workflow —
# see module docstring for why this reimplements those rules client-side
# instead of calling labclaude for them).
# ---------------------------------------------------------------------

_EXP_FLOW_RE = re.compile(r"<exp_flow>(.*?)</exp_flow>", re.DOTALL)

# Mirrors labclaude's workflow/ot2_compile.py::OT2_PIPETTE_OPS.
_OT2_PIPETTE_OPS = {"pick_up_tip", "drop_tip", "return_tip", "aspirate",
                    "dispense", "mix", "blow_out", "move", "home"}
_OT2_MOUNT_OPS = _OT2_PIPETTE_OPS - {"home"}
_OT2_VOLUME_OPS = {"aspirate", "dispense", "mix"}


def extract_json(content: str) -> tuple[dict | None, str]:
    try:
        match = _EXP_FLOW_RE.search(content)
        payload = match.group(1).strip() if match else content.strip()
        if payload.startswith("```"):
            payload = re.sub(r"^```json?\s*", "", payload)
            payload = re.sub(r"\s*```$", "", payload)
        return json.loads(payload), ""
    except Exception as e:
        return None, f"JSONError: {e}"


def check_workflow_structure(
    wf: dict[str, Any], device_catalog: dict[str, set[str]],
) -> list[RuleViolation]:
    """Local, offline structural check. ``halt`` violations mean the
    workflow would be rejected by labclaude's ``compile_workflow`` (or,
    for the OT-2 param/fusion checks, would compile but very likely
    misbehave at run time); ``warn`` violations are surfaced for the human
    reviewer but don't block commit on their own.
    """
    violations: list[RuleViolation] = []

    def halt(msg: str) -> None:
        violations.append(RuleViolation("K_p_structure", "halt", msg))

    def warn(msg: str) -> None:
        violations.append(RuleViolation("K_p_structure", "warn", msg))

    if not wf.get("name"):
        halt("workflow missing 'name'")

    steps = wf.get("steps")
    if not isinstance(steps, list) or not steps:
        halt("workflow 'steps' must be a non-empty list")
        return violations

    ids: list[str] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            halt(f"step {i} is not an object")
            ids.append(f"s{i + 1}")
            continue
        sid = s.get("id") or f"s{i + 1}"
        if sid in seen_ids:
            halt(f"duplicate step id {sid!r}")
        seen_ids.add(sid)
        ids.append(sid)
        for key in ("device_type", "command"):
            if not s.get(key):
                halt(f"step {sid} missing {key!r}")

    # Dependency references + self-dependency.
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or ids[i]
        for dep in s.get("depends_on") or []:
            if dep not in seen_ids:
                halt(f"step {sid} depends_on unknown step {dep!r}")
            elif dep == sid:
                halt(f"step {sid} cannot depend on itself")

    # Cycle detection (DFS), mirrors ir.py::_validate_acyclic.
    graph = {(s.get("id") or ids[i]): (s.get("depends_on") or [])
             for i, s in enumerate(steps) if isinstance(s, dict)}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in graph}

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for dep in graph.get(node, []):
            if dep not in color:
                continue  # already reported as an unknown reference above
            if color[dep] == GRAY:
                cycle = path[path.index(dep):] + [dep]
                halt("dependency cycle: " + " -> ".join(cycle))
            elif color[dep] == WHITE:
                visit(dep, path + [dep])
        color[node] = BLACK

    for sid in graph:
        if color[sid] == WHITE:
            visit(sid, [sid])

    # Device type / command registration, against the live catalog.
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or ids[i]
        dt, cmd = s.get("device_type"), s.get("command")
        if not dt or not cmd:
            continue  # already halted above
        if dt not in device_catalog:
            halt(f"step {sid}: unknown device_type {dt!r} "
                f"(available: {sorted(device_catalog)})")
            continue
        if cmd not in device_catalog[dt]:
            halt(f"step {sid}: device_type {dt!r} has no command {cmd!r} "
                f"(available: {sorted(device_catalog[dt])})")

    # OT-2 parameter-shape checks.
    ot2_steps = [(i, s) for i, s in enumerate(steps)
                if isinstance(s, dict) and s.get("device_type") == "ot2"]
    for i, s in ot2_steps:
        sid = s.get("id") or ids[i]
        cmd = s.get("command")
        params = s.get("params") or {}
        if cmd in _OT2_MOUNT_OPS:
            mount = params.get("mount")
            if mount not in ("left", "right"):
                halt(f"OT-2 step {sid} ({cmd}): params.mount must be "
                    f"'left' or 'right' (got {mount!r})")
        if cmd in _OT2_MOUNT_OPS - {"return_tip", "drop_tip"}:
            slot = params.get("slot")
            if slot is not None and not (isinstance(slot, int) and 1 <= slot <= 12):
                halt(f"OT-2 step {sid} ({cmd}): params.slot must be an "
                    f"int 1-12 (got {slot!r})")
        if cmd in _OT2_VOLUME_OPS:
            vol = params.get("volume")
            if not (isinstance(vol, (int, float)) and not isinstance(vol, bool) and vol > 0):
                halt(f"OT-2 step {sid} ({cmd}): params.volume must be a "
                    f"positive number (got {vol!r})")

    # Fusion-chaining check (see module docstring): consecutive OT-2
    # pipetting steps that don't strictly chain still work, just as
    # separate protocol dispatches — a warning, not a hard error.
    prev_id: str | None = None
    for i, s in ot2_steps:
        sid = s.get("id") or ids[i]
        if s.get("command") not in _OT2_PIPETTE_OPS:
            prev_id = None
            continue
        deps = s.get("depends_on") or []
        if prev_id is not None and deps != [prev_id]:
            warn(f"OT-2 step {sid} does not strictly chain onto the "
                f"immediately preceding OT-2 step {prev_id!r} "
                f"(depends_on={deps}) — labclaude will dispatch it as a "
                "separate OT-2 protocol run instead of batching it with "
                "its neighbors; fine if intentional, wasteful if not")
        prev_id = sid

    return violations


def validate_machine_code_labclaude(
    content: str, device_catalog: dict[str, set[str]],
) -> tuple[bool, str, list[RuleViolation]]:
    """Drop-in replacement for verification.engine.validate_machine_code,
    same (ok, message, violations) contract for the existing Rectify loop.
    Purely local — see module docstring for why this doesn't round-trip
    through labclaude's API during the loop."""
    data, err = extract_json(content)
    if err:
        return False, err, []
    if not isinstance(data, dict):
        return False, "SchemaError: top-level JSON must be an object", []

    violations = check_workflow_structure(data, device_catalog)
    halts = [v for v in violations if v.severity == "halt"]
    if halts:
        return False, "; ".join(v.message for v in halts), violations
    msg = "Structural validation passed (labclaude target)."
    if violations:
        msg += f" {len(violations)} warning(s) — see rule_violations."
    return True, msg, violations


# ---------------------------------------------------------------------
# Commit — only called after validate_machine_code_labclaude passes AND a
# human has reviewed the rendered workflow (see run_labclaude_agent.py).
# The public shell has no equivalent (it never deploys anything); this is
# what actually makes the generated plan runnable. POST /api/workflows
# re-validates server-side (compile_workflow) before persisting — the
# authoritative check, this call is not just a formality.
# ---------------------------------------------------------------------

def commit_machine_code_labclaude(
    content: str, labclaude_cfg: LabclaudeConfig,
) -> dict[str, Any]:
    data, err = extract_json(content)
    if err:
        raise ValueError(err)

    r = requests.post(f"{labclaude_cfg.base_url.rstrip('/')}/api/workflows",
                      headers={"Authorization": f"Bearer {labclaude_cfg.token}"},
                      json={"name": data["name"], "steps": data["steps"]}, timeout=10)
    r.raise_for_status()
    return r.json()


def render_workflow_for_review(wf: dict[str, Any]) -> str:
    """Human-readable rendering of a workflow for the mandatory review gate
    (see run_labclaude_agent.py) — one line per step in declared order,
    showing id/device/command/params/depends_on so a reviewer can actually
    read the plan instead of raw JSON."""
    lines = [f"Workflow: {wf.get('name', '(unnamed)')}"]
    for i, s in enumerate(wf.get("steps") or []):
        if not isinstance(s, dict):
            lines.append(f"  {i + 1}. <malformed step: {s!r}>")
            continue
        deps = s.get("depends_on") or []
        dep_txt = f" [after: {', '.join(deps)}]" if deps else ""
        params = s.get("params") or {}
        param_txt = ", ".join(f"{k}={v!r}" for k, v in params.items())
        lines.append(
            f"  {i + 1}. [{s.get('id')}] {s.get('name') or s.get('id')} — "
            f"{s.get('device_type')}.{s.get('command')}({param_txt}){dep_txt}"
        )
    return "\n".join(lines)
