"""Command line interface for the live airline recovery benchmark."""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ARGUMENT_WIDTH = 60
# Width of every demo column except the argument summary, including separators.
FIXED_WIDTH = 60


def _print_json(value, **options):
    print(json.dumps(value, sort_keys=True, **options), flush=True)


def _validate_live_destination(parser, output):
    """Only write into an empty directory or a previously generated task directory."""
    destination = Path(output)
    if not destination.exists():
        return
    if not destination.is_dir():
        parser.error("--output must be a task directory")
    if not any(destination.iterdir()):
        return
    try:
        manifest = json.loads((destination / "manifest.json").read_text())
        tasks = manifest["tasks"]
        recognized = (
            isinstance(manifest.get("version"), str)
            and isinstance(tasks, list)
            and bool(tasks)
            and all(isinstance(task, dict) and {"path", "split", "index"} <= set(task) for task in tasks)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        recognized = False
    if not recognized:
        parser.error("--output contains unrecognized files; choose an empty directory or an existing live-tasks output")


def _summarize_arguments(arguments, width=ARGUMENT_WIDTH):
    """One-line `key=value` rendering of tool arguments, truncated to `width`."""
    if not arguments:
        return "-"
    text = ", ".join(f"{key}={value if isinstance(value, str) else json.dumps(value, sort_keys=True)}"
                     for key, value in sorted(arguments.items()))
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 3] + "..."


def _demo_row(step, tool, arguments, result, summary, width):
    verified = f"{summary['verification_windows']}/{summary['required_verification_windows']}"
    return (f"{step:>4}  {tool:<17}  {arguments:<{width}}  {result:<6}  "
            f"{summary['pending_bookings']:>7}  {summary['outbox_pending']:>6}  {verified:>8}").rstrip()


def _run_demo(args):
    from .live.environment import LiveAirlineEnv
    from .live.evaluate import reset_options
    from .live.policies import load_policy
    tier = getattr(args, "tier", "easy")
    task = f"airline-recovery-{'' if tier == 'easy' else tier + '-'}{args.split}-{args.index:03d}"
    # The reference policy solves the easy cases; the oracle solves the generated hard ones.
    policy_name = "reference" if tier == "easy" else "oracle"
    columns = shutil.get_terminal_size(fallback=(FIXED_WIDTH + ARGUMENT_WIDTH, 24)).columns
    width = max(20, min(ARGUMENT_WIDTH, columns - FIXED_WIDTH))
    with (LiveAirlineEnv() if tier == "easy" else LiveAirlineEnv(max_steps=None)) as env:
        observation, _ = env.reset(seed=args.seed, options=reset_options(args.split, args.index, tier))
        policy = load_policy(policy_name)
        workers = {name: process.pid for name, process in env.stack._processes.items()}
        if args.json:
            start = {"event": "start", "task": task, "split": args.split, "index": args.index,
                     "seed": args.seed, "workers": workers, "summary": observation["summary"]}
            if tier != "easy":
                start.update(tier=tier, level=observation.get("level"), policy=policy_name)
            _print_json(start)
        else:
            print("Airline Recovery Env: scripted recovery demo", flush=True)
            print(f"Task: {task}   Seed: {args.seed}   Policy: scripted {policy_name}", flush=True)
            print(f"Services: {', '.join(sorted(workers))} ({len(workers)} local HTTP workers, synthetic data)\n", flush=True)
            header = (f"{'step':>4}  {'tool':<17}  {'arguments':<{width}}  {'result':<6}  "
                      f"{'pending':>7}  {'outbox':>6}  {'verified':>8}")
            print(header, flush=True)
            print("-" * len(header), flush=True)
            print(_demo_row("--", "(initial state)", "-", "-", observation["summary"], width), flush=True)
        while not env.done:
            action = policy(observation)
            observation, reward, terminated, truncated, info = env.step(action)
            ok = bool(observation["result"]["ok"])
            if args.json:
                _print_json({"event": "step", "step": observation["step"], "action": action, "ok": ok,
                             "summary": observation["summary"]})
            else:
                print(_demo_row(f"{observation['step']:02d}", action["tool"],
                                _summarize_arguments(action["arguments"], width),
                                "ok" if ok else "failed", observation["summary"], width), flush=True)
            if args.delay and not env.done:
                time.sleep(args.delay)
        score = info["score"]
    if args.json:
        _print_json({"event": "outcome", "solved": bool(score["success"]), "score": score})
        return
    violations = score["details"]["violations"]
    print("\npending = bookings awaiting completion, outbox = undelivered events, "
          "verified = healthy verification windows", flush=True)
    print(f"\nOutcome: {'SOLVED' if score['success'] else 'NOT SOLVED'}", flush=True)
    print(f"  reward: {score['reward']}", flush=True)
    print(f"  steps:  {score['details']['steps']}", flush=True)
    if violations:
        print(f"  integrity violations ({len(violations)}):", flush=True)
        for violation in violations:
            print(f"    {violation}", flush=True)


def main(argv=None):
    try:
        _dispatch(argv)
    except BrokenPipeError:
        # The reader went away (for example `airline-recovery demo --json | head`); leave quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)


def _dispatch(argv):
    parser = argparse.ArgumentParser(
        prog="airline-recovery",
        description="Stateful airline recovery: run and inspect the live HTTP benchmark.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tier_help = "task tier: easy (default) or the generated hard tier"
    listing = sub.add_parser("list", help="List the benchmark tasks", description="List the public task manifest by split.")
    listing.add_argument("--tier", choices=["easy", "hard", "all"], default="easy", help=tier_help + ", or all for both")
    demo = sub.add_parser("demo", help="Watch a scripted recovery of live HTTP services", description="Run the scripted reference policy (or the hard-tier oracle) against local HTTP service workers and show each step.")
    demo.add_argument("--tier", choices=["easy", "hard"], default="easy", help=tier_help)
    demo.add_argument("--split", choices=["train", "eval", "test"], default="train")
    demo.add_argument("--index", type=int, default=0)
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--delay", type=float, default=0.2, help="seconds to pause between steps (default: 0.2)")
    demo.add_argument("--json", action="store_true", help="emit one JSON object per line instead of the table")
    showcase = sub.add_parser("showcase", help="Compare payment recovery strategies and save the outcomes")
    showcase.add_argument("--seed", type=int, default=42)
    showcase.add_argument("--delay", type=float, default=0.15)
    showcase.add_argument("--output", default="runs/showcase")
    showcase.add_argument("--overwrite", action="store_true", help="replace results already in --output")
    harbor = sub.add_parser("build-harbor", help="Generate Harbor tasks", description="Generate a Harbor task for every benchmark split.")
    harbor.add_argument("--output", default="live-tasks", help="task destination (default: live-tasks)")
    harbor.add_argument("--seed", type=int, default=None,
                        help="pin every task to one seed for debugging (default: a fresh seed per run)")
    harbor.add_argument("--tier", choices=["easy", "hard", "all"], default="easy",
                        help=tier_help + ", or all for both; hard tasks are written under <output>/hard/")
    args = parser.parse_args(argv)
    if args.command in {"demo", "showcase"} and not 0 <= args.delay <= 10:
        parser.error("--delay must be between 0 and 10 seconds")
    if args.command == "showcase":
        from .live.showcase import run_showcase
        try:
            run_showcase(args.output, seed=args.seed, delay=args.delay, overwrite=args.overwrite)
        except (FileExistsError, NotADirectoryError) as error:
            parser.error(f"--output {args.output}: {error}")
    elif args.command == "demo":
        from .live.evaluate import manifest_for
        try:
            manifest = manifest_for(args.tier)
        except ValueError as error:
            parser.error(str(error))
        if args.index not in {task["index"] for task in manifest[args.split]}:
            parser.error(f"no task at --tier {args.tier} --split {args.split} --index {args.index}; see `airline-recovery list --tier {args.tier}`")
        _run_demo(args)
    elif args.command == "list":
        from .live.evaluate import manifest_for
        try:
            if args.tier == "all":
                _print_json({tier: manifest_for(tier) for tier in ("easy", "hard")}, indent=2)
            else:
                _print_json(manifest_for(args.tier), indent=2)
        except ValueError as error:
            parser.error(str(error))
    else:
        from .live.harbor import build
        _validate_live_destination(parser, args.output)
        try:
            # The easy call is unchanged so that existing automation sees the same arguments.
            result = build(args.output, args.seed) if args.tier == "easy" else build(args.output, args.seed, args.tier)
        except ValueError as error:
            parser.error(str(error))
        _print_json(result, indent=2)


if __name__ == "__main__":
    main()
