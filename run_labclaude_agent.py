"""Headless CLI: turn a protocol into a labclaude workflow + OT-2 protocol.

There is no web UI for this (see the module docstring of
``src/capabilities/targets/labclaude_ot2.py`` for why: LLM credentials live
in ``data/keys.env``, read once at process startup by ``config/settings.py``).
This script is the actual entry point until/unless a UI is built on top of it.

Usage:
    cp data/keys.env.example data/keys.env
    # fill in MODEL_NAME / MODEL_API_KEY / MODEL_BASE_URL (e.g. DeepSeek — see
    # comments in keys.env.example) and LABCLAUDE_OT2_TARGET=1 in keys.env

    # labclaude backend and OT-2 Deck Studio must already be running locally
    # (or set LABCLAUDE_BASE_URL/OT2_BASE_URL to point elsewhere), and you need
    # a labclaude session token (POST /api/login as the dedicated "bioproagent"
    # service account — see comment in keys.env.example; do not log in as a
    # human account, that would misattribute the workflow's provenance) and an
    # OT-2 API token (Deck Studio: 设置 → API 设置 → 访问令牌) — put them in
    # keys.env too (LABCLAUDE_TOKEN / OT2_TOKEN).

    python run_labclaude_agent.py --protocol-file my_protocol.txt \
        --exp-info "ELISA, 96-well plate, room temperature"

What it does: creates one ProAgent session, drives the existing
Design→Verify→Rectify loop (align_draft_to_automation → generate_machine_code
→ validate_machine_code, retrying on failure) via a single ``process_query``
call, then — only if validation actually passed — commits the result to the
running labclaude + OT-2 Deck Studio instances. If it doesn't pass within the
loop's retry budget, nothing is written and the last errors are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--protocol-file", help="path to a text file containing the protocol")
    ap.add_argument("--protocol", help="protocol text inline (alternative to --protocol-file)")
    ap.add_argument("--exp-info", default="", help="short experiment context/metadata")
    args = ap.parse_args(argv)

    if not args.protocol_file and not args.protocol:
        ap.error("pass --protocol-file or --protocol")

    protocol_text = args.protocol
    if args.protocol_file:
        with open(args.protocol_file, encoding="utf-8") as f:
            protocol_text = f.read()

    if os.environ.get("LABCLAUDE_OT2_TARGET", "").strip() in ("", "0", "false", "False"):
        print("LABCLAUDE_OT2_TARGET is not set (or is falsy) in your environment/keys.env — "
              "the agent would use the generic nodes/consumables/connections schema instead "
              "of labclaude's, which is not what this script is for. Set "
              "LABCLAUDE_OT2_TARGET=1 in data/keys.env and retry.", file=sys.stderr)
        return 2

    # Imported after the env check above (these imports build LLM clients from
    # config/settings.py at import time — no point paying that cost just to
    # print a usage error).
    from main_evaluate import ProAgent
    from src.capabilities.targets.labclaude_ot2 import commit_machine_code_labclaude_ot2
    from src.tools.tool_definitions import _labclaude_ot2_configs

    agent = ProAgent(eval_mode=False)
    state = agent._create_session()

    instruction = (
        "Design an automated experiment plan for the following protocol, "
        "align it to the available automation platform, generate machine code, "
        "and validate it.\n\n"
        f"Experiment info: {args.exp_info or '(none given)'}\n\n"
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

    print("\n=== Validation passed. Committing to labclaude + OT-2 Deck Studio ===")
    labclaude_cfg, ot2_cfg = _labclaude_ot2_configs()
    result = commit_machine_code_labclaude_ot2(machine_code, labclaude_cfg, ot2_cfg)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
