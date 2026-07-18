"""Headless CLI: turn a protocol into a labclaude workflow.

There is no web UI for this (see the module docstring of
``src/capabilities/targets/labclaude_ot2.py`` for why: LLM credentials live
in ``data/keys.env``, read once at process startup by ``config/settings.py``).
This script is the actual entry point until/unless a UI is built on top of it.

Usage:
    cp data/keys.env.example data/keys.env
    # fill in MODEL_NAME / MODEL_API_KEY / MODEL_BASE_URL (e.g. DeepSeek — see
    # comments in keys.env.example) and LABCLAUDE_TARGET=1 in keys.env

    # labclaude backend must already be running locally (or set
    # LABCLAUDE_BASE_URL to point elsewhere), and you need a labclaude
    # session token (POST /api/login as the dedicated "bioproagent" service
    # account — see comment in keys.env.example; do not log in as a human
    # account, that would misattribute the workflow's provenance) — put it in
    # keys.env too (LABCLAUDE_TOKEN).

    python run_labclaude_agent.py --protocol-file my_protocol.txt \
        --exp-info "ELISA, 96-well plate, room temperature"

What it does: creates one ProAgent session, drives the existing
Design→Verify→Rectify loop (align_draft_to_automation → generate_machine_code
→ validate_machine_code, retrying on failure) via a single ``process_query``
call. If validation didn't pass within the loop's retry budget, nothing is
written and the last errors are printed. If it did pass, the generated
workflow is rendered step-by-step and this script **stops and asks for
explicit human confirmation** before committing anything to the running
labclaude instance — passing structural validation is necessary but not
sufficient; a person still reads the plan before it becomes real (see
``_confirm_and_commit`` below). Pass ``--yes`` to skip the prompt for
scripted/non-interactive use — only do that once you trust the pipeline,
since it removes the human check this script exists to enforce.

Optional: BayesOpt-suggested experiment parameters
---------------------------------------------------
Pass ``--bayesopt-study NAME`` to have this script ask a running
`bayesopt-agent <https://github.com/BinzhangBJ/bayesopt-agent>`_ service for
the next parameter suggestion (via ``src/capabilities/targets/bayesopt.py``)
and fold it into ``exp_info`` before the FSM ever runs — see that module's
docstring for why this is a driving-script step, not a new FSM tool. First
use of a study also needs ``--bayesopt-param-space`` (a JSON list of
``{"name", "type", "low"/"high" or "choices"}``, see bayesopt-agent's README)
and optionally ``--bayesopt-direction`` (default ``min``).

    python run_labclaude_agent.py --protocol-file my_protocol.txt \
        --bayesopt-study elisa-temp-cycles \
        --bayesopt-param-space '[{"name":"temperature","type":"float","low":20,"high":40},
                                  {"name":"cycles","type":"int","low":1,"high":10}]'

This only fetches a *suggestion* and folds it into the prompt — the real
objective value doesn't exist yet (the wet-lab run hasn't happened). Once it
does, call this script again in its other mode to feed the result back,
skipping the FSM entirely:

    python run_labclaude_agent.py --bayesopt-study elisa-temp-cycles \
        --bayesopt-observe-params '{"temperature": 37.2, "cycles": 5}' \
        --bayesopt-observe-objective 0.87

Requires ``BAYESOPT_TARGET=1``, ``BAYESOPT_BASE_URL``, ``BAYESOPT_TOKEN`` in
``data/keys.env`` (see ``keys.env.example``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _bayesopt_config_from_env():
    from src.capabilities.targets.bayesopt import BayesOptConfig
    if os.environ.get("BAYESOPT_TARGET", "").strip() in ("", "0", "false", "False"):
        print("BAYESOPT_TARGET is not set (or is falsy) in your environment/keys.env — "
              "set BAYESOPT_TARGET=1 (plus BAYESOPT_BASE_URL/BAYESOPT_TOKEN) to use "
              "--bayesopt-study.", file=sys.stderr)
        return None
    return BayesOptConfig(
        base_url=os.environ.get("BAYESOPT_BASE_URL", "http://127.0.0.1:8100"),
        token=os.environ.get("BAYESOPT_TOKEN", ""),
    )


def _run_bayesopt_observe(args) -> int:
    """The "after the real experiment finished" half — feeds a measured
    objective back, no FSM/LLM involved at all."""
    cfg = _bayesopt_config_from_env()
    if cfg is None:
        return 2
    from src.capabilities.targets.bayesopt import observe

    try:
        params = json.loads(args.bayesopt_observe_params)
    except json.JSONDecodeError as e:
        print(f"--bayesopt-observe-params is not valid JSON: {e}", file=sys.stderr)
        return 2

    result = observe(cfg, args.bayesopt_study, params, args.bayesopt_observe_objective)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--protocol-file", help="path to a text file containing the protocol")
    ap.add_argument("--protocol", help="protocol text inline (alternative to --protocol-file)")
    ap.add_argument("--exp-info", default="", help="short experiment context/metadata")
    ap.add_argument("--bayesopt-study", help="bayesopt-agent study name (optional)")
    ap.add_argument("--bayesopt-param-space",
                    help="JSON param space list, needed on a study's first use")
    ap.add_argument("--bayesopt-direction", default="min", choices=["min", "max"],
                    help="optimization direction if creating the study (default: min)")
    ap.add_argument("--bayesopt-observe-params",
                    help="JSON params dict to report back (switches to observe-only mode, "
                         "no FSM/LLM run)")
    ap.add_argument("--bayesopt-observe-objective", type=float,
                    help="measured objective value to report back (with "
                         "--bayesopt-observe-params)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the human-confirmation prompt and commit automatically if "
                         "validation passed. Only use this once you trust the pipeline — "
                         "it removes the human review step this script exists to enforce.")
    args = ap.parse_args(argv)

    if args.bayesopt_observe_params or args.bayesopt_observe_objective is not None:
        if not args.bayesopt_study or not args.bayesopt_observe_params or \
                args.bayesopt_observe_objective is None:
            ap.error("--bayesopt-observe-params/--bayesopt-observe-objective need "
                     "--bayesopt-study and each other")
        return _run_bayesopt_observe(args)

    if not args.protocol_file and not args.protocol:
        ap.error("pass --protocol-file or --protocol")

    protocol_text = args.protocol
    if args.protocol_file:
        with open(args.protocol_file, encoding="utf-8") as f:
            protocol_text = f.read()

    if os.environ.get("LABCLAUDE_TARGET", "").strip() in ("", "0", "false", "False"):
        print("LABCLAUDE_TARGET is not set (or is falsy) in your environment/keys.env — "
              "the agent would use the generic nodes/consumables/connections schema instead "
              "of labclaude's, which is not what this script is for. Set "
              "LABCLAUDE_TARGET=1 in data/keys.env and retry.", file=sys.stderr)
        return 2

    exp_info = args.exp_info
    suggested_params = None
    if args.bayesopt_study:
        bo_cfg = _bayesopt_config_from_env()
        if bo_cfg is None:
            return 2
        from src.capabilities.targets.bayesopt import (
            create_study_if_absent, format_suggestion_for_prompt, suggest,
        )

        if args.bayesopt_param_space:
            try:
                param_space = json.loads(args.bayesopt_param_space)
            except json.JSONDecodeError as e:
                print(f"--bayesopt-param-space is not valid JSON: {e}", file=sys.stderr)
                return 2
            create_study_if_absent(bo_cfg, args.bayesopt_study, args.bayesopt_direction,
                                   param_space)
        suggested_params = suggest(bo_cfg, args.bayesopt_study)
        suggestion_text = format_suggestion_for_prompt(suggested_params)
        print(f"=== BayesOpt suggested parameters for study {args.bayesopt_study!r}: "
              f"{suggestion_text} ===")
        exp_info = f"{exp_info}\nSuggested parameters (from BayesOpt): {suggestion_text}" \
            if exp_info else f"Suggested parameters (from BayesOpt): {suggestion_text}"

    # Imported after the env checks above (these imports build LLM clients from
    # config/settings.py at import time — no point paying that cost just to
    # print a usage error).
    from main_evaluate import ProAgent
    from src.capabilities.targets.labclaude_ot2 import (
        commit_machine_code_labclaude, extract_json, render_workflow_for_review,
    )
    from src.tools.tool_definitions import _labclaude_ot2_configs

    agent = ProAgent(eval_mode=False)
    state = agent._create_session()

    instruction = (
        "Design an automated experiment plan for the following protocol, "
        "align it to the available automation platform, generate machine code, "
        "and validate it.\n\n"
        f"Experiment info: {exp_info or '(none given)'}\n\n"
        f"Protocol:\n{protocol_text}"
    )

    print("=== Running Design -> Verify -> Rectify loop ===")
    response = agent.process_query(instruction, state)
    print(response)

    machine_code = state.mem_work.get("machine_code")
    kp = state.mem_work.get("kp_verification") or {}
    if not machine_code or kp.get("status") != "success":
        print("\n=== Not committed: validation did not pass within the loop's retry budget ===")
        print(json.dumps(kp, indent=2, ensure_ascii=False))
        return 1

    print("\n=== Validation passed. Human review required before committing ===")
    wf, err = extract_json(machine_code)
    if err:
        # Shouldn't happen — validation already parsed this successfully — but
        # don't silently skip the review gate on a surprise, refuse instead.
        print(f"Could not re-parse machine_code for review ({err}); refusing to commit "
              "without a reviewable rendering.", file=sys.stderr)
        return 1
    print(render_workflow_for_review(wf))
    if kp.get("rule_violations"):
        warns = [v for v in kp["rule_violations"] if v.get("severity") == "warn"]
        if warns:
            print("\nWarnings (non-blocking, but read before approving):")
            for v in warns:
                print(f"  - {v['message']}")

    if not args.yes:
        answer = input("\nCommit this workflow to labclaude? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not committed (declined at human review).")
            return 1
    else:
        print("\n--yes passed: skipping confirmation prompt.")

    labclaude_cfg = _labclaude_ot2_configs()
    result = commit_machine_code_labclaude(machine_code, labclaude_cfg)
    print("\n=== Committed ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if suggested_params is not None:
        print(f"\n=== Once this experiment's real result is measured, report it back with: ===\n"
              f"python {sys.argv[0]} --bayesopt-study {args.bayesopt_study} "
              f"--bayesopt-observe-params '{json.dumps(suggested_params)}' "
              f"--bayesopt-observe-objective <measured value>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
