"""Generate Harbor tasks with a private live sidecar that signs the outcome.

The sidecar world runs the one authoritative episode: its actions cannot be
undone or retried. The verifier checks the sidecar's HMAC-signed receipt and
never trusts an agent-written score or action list.
"""
import argparse
import json
import secrets
import shutil
from pathlib import Path

from .. import __version__
from .evaluate import EASY_MAX_STEPS, TIERS, check_tier, manifest_for
from .scenarios import CASES

IMAGE="python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
CORE=("__init__.py","store.py","worker.py","runtime.py","scenarios.py","verification.py","environment.py","bridge.py")
# The hard tier's generator and injector; the world image needs them for hard tasks only.
HARD_CORE=("hardcases.py","hard_injector.py")
RECEIPT_SCHEMA={"easy":3,"hard":4}

CONTROL='''import json,os,sys,urllib.error,urllib.request
from pathlib import Path
ROOT=os.environ.get("AIRLINE_RECOVERY_WORLD","http://world:8081")
STORE=Path(__file__).resolve().with_name("episode.json")
def call(action=None):
    path="/observation" if action is None else "/step"
    req=urllib.request.Request(ROOT+path,data=None if action is None else json.dumps(action).encode(),headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=60) as response: result=json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit("World rejected the request: "+error.read().decode(errors="replace"))
    # The receipt is saved from any response, so a lost reply to the final step can be recovered by re-reading the observation.
    state=json.loads(STORE.read_text())
    if action is not None: state["actions"].append(action)
    if "receipt" in result: state["receipt"]=result["receipt"]
    STORE.write_text(json.dumps(state))
    return result
if __name__=="__main__":
    action=json.loads(sys.argv[2]) if len(sys.argv)==3 and sys.argv[1]=="action" else None
    print(json.dumps(call(action),indent=2))
'''

ORACLE='''import sys
sys.path.insert(0,"/app")
from control import call
from {module} import {policy}
policy={policy}()
transition=call()
while not (transition["terminated"] or transition["truncated"]):
    transition=call(policy(transition["observation"]))
print(transition["reward"])
'''
# Which bundled policy solves a tier, and which files the solution image needs for it.
SOLUTION={"easy":("policies","ReferencePolicy",("policies.py","oracle.py")),
          "hard":("oracle","OraclePolicy",("policies.py","oracle.py"))}

GRADE='''import hashlib,hmac,json,math,os,stat
from pathlib import Path
output=Path("/logs/verifier"); output.mkdir(parents=True,exist_ok=True)
reward={"reward":0.0,"success":0.0}; details={}
try:
    # Per-task file names: a cached build context of another task can never stand in for this one.
    specs=sorted(Path("/tests/").glob("*.spec.json")); keys=sorted(Path("/tests/").glob("*.receipt.key"))
    if len(specs)!=1 or len(keys)!=1 or specs[0].name.removesuffix(".spec.json")!=keys[0].name.removesuffix(".receipt.key"):
        raise ValueError("Expected exactly one task spec and its receipt key in /tests")
    spec=json.loads(specs[0].read_text())
    key=keys[0].read_bytes().strip()
    fd=os.open("/app/episode.json",os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as handle:
        meta=os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size>1000000: raise ValueError("Invalid artifact")
        artifact=json.load(handle,parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    receipt=artifact.get("receipt") if isinstance(artifact,dict) else None
    if not isinstance(receipt,dict) or not isinstance(receipt.get("body"),dict) or not isinstance(receipt.get("signature"),str):
        raise ValueError("No signed receipt: the episode must end with finish or exhaust its action budget")
    body=receipt["body"]
    expected=hmac.new(key,json.dumps(body,sort_keys=True,separators=(",",":"),allow_nan=False).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,receipt["signature"]): raise ValueError("Receipt signature does not verify")
    tier=spec.get("tier","easy")
    if body.get("schema_version")!={"easy":3,"hard":4}[tier] or body.get("tier","easy")!=tier or body.get("task")!={"split":spec["split"],"index":spec["index"]}: raise ValueError("Receipt is for another task")
    score=body["score"]; value=score["reward"]
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1: raise ValueError("Invalid receipt reward")
    details=body
    reward={"reward":float(value),"success":float(score["success"] is True)}
except Exception as error:
    details={"error":type(error).__name__+": "+str(error)}
(output/"details.json").write_text(json.dumps(details,indent=2))
(output/"reward.json").write_text(json.dumps(reward))
'''


EXPLICIT_HARD_RULES = '''
The rules that decide the grade, in plain words (the episode contract has the exact definitions):
- A customer who cancelled must end cancelled: never confirm, seat, check in or charge that request, and refund any charge that was captured (void_booking cancels and refunds).
- Each confirmed booking has exactly one charge captured at the payment provider. A retry with the same client_reference is the same customer, not a new sale: never give it a second booking, seat or charge.
- Book every customer on the flight, passenger and fare they requested and were quoted. Do not change prices or settings in a way that books anyone at a different fare.
- A provider payment can stay pending and settle later; pending is not captured. Before completing a stuck booking, confirm its payment at the provider (provider_lookup, limited quota) or in durable rows. Alerts, logs and metrics can be stale or wrong.
- Do not change bookings accepted before the incident, and do not quarantine valid events.
- After you finish, ordinary customer traffic is replayed against the settings you leave deployed: lost payment acknowledgements that the customer retries, fare changes, a cancellation followed by a new purchase, a retry after an idempotency key has expired, and an event delivered twice.
A sound order of work: inspect, fix the underlying cause, give every affected request its correct outcome, then obtain two healthy probe windows after your last change and call finish. Each read, change and provider lookup adds a small cost (see the cost rule).
'''


def instruction(control="python /app/control.py", store="/app/episode.json", tier="easy", budget=None, explicit=False):
    """Agent-facing task text, shared by the Harbor tasks and the external-agent runner.

    The easy text is unchanged from before tiers existed. The hard text states the
    case's budget and keeps only the mechanics: no hints about what to inspect,
    what to preserve or how to verify. ``explicit=True`` appends the integrity rules
    in plain words and a general order of work; it exists for the instruction-clarity
    ablation and is not used by the published tasks.
    """
    check_tier(tier)
    if explicit and tier == "easy":
        raise ValueError("the explicit instruction variant exists only for the hard tier")
    if tier=="easy":
        budget=EASY_MAX_STEPS if budget is None else budget
        return f'''Recover a synthetic airline booking-to-checkin system running in separate HTTP worker processes.
Preserve accepted bookings, exact single charges, seat capacity, accepted fares, and valid events.
Run `{control}` for the current observation and full tool schemas.
Run `{control} action '{{"tool":"get_metrics","arguments":{{}}}}'` to act.
Inspect logs/config/SQL, repair underlying problems, reconcile accepted transactions and verify two healthy probe windows before finish.
Every tool action advances customer traffic; another incident can arrive while you work. Budget: {budget} actions.
There is one episode and its actions cannot be undone: the world runs in an isolated sidecar and is the only thing graded.
When you call finish (or exhaust the budget) the sidecar issues a signed receipt, which the CLI stores in {store}. An episode without a receipt scores 0.
Reward measures actual completed requests and integrity; correct configuration names or a written explanation do not earn credit.
This benchmark executes local HTTP and SQLite transactions; it does not connect to real airlines, payments or cloud infrastructure.
'''
    if type(budget) is not int or budget<1:
        raise ValueError("the hard instruction needs the case's action budget")
    text = f'''Recover a synthetic airline booking-to-checkin system running in separate HTTP worker processes.
Run `{control}` for the current observation, the tool schemas, the configuration contracts and the episode contract.
Run `{control} action '{{"tool":"get_metrics","arguments":{{}}}}'` to act.
Every tool action advances customer traffic. Budget: {budget} actions.
Alerts, logs and metrics are best-effort and may be stale or wrong; durable business rows and the payment provider are authoritative.
There is one episode and its actions cannot be undone: the world runs in an isolated sidecar and is the only thing graded.
When you call finish (or exhaust the budget) the sidecar issues a signed receipt, which the CLI stores in {store}. An episode without a receipt scores 0.
Reward measures the outcome of every customer request, the integrity rules in the episode contract and the cost of your actions; correct configuration names or a written explanation do not earn credit.
This benchmark executes local HTTP and SQLite transactions; it does not connect to real airlines, payments or cloud infrastructure.
'''
    return text + EXPLICIT_HARD_RULES if explicit else text


def verify_receipt(artifact, key, split, index, tier="easy"):
    """Return (reward, success, body) for a signed receipt, or raise ValueError. Mirrors grade.py."""
    import hashlib, hmac, math
    check_tier(tier)
    receipt=artifact.get("receipt") if isinstance(artifact,dict) else None
    if not isinstance(receipt,dict) or not isinstance(receipt.get("body"),dict) or not isinstance(receipt.get("signature"),str):
        raise ValueError("No signed receipt: the episode must end with finish or exhaust its action budget")
    body=receipt["body"]
    expected=hmac.new(key,json.dumps(body,sort_keys=True,separators=(",",":"),allow_nan=False).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,receipt["signature"]): raise ValueError("Receipt signature does not verify")
    if body.get("schema_version")!=RECEIPT_SCHEMA[tier] or body.get("tier","easy")!=tier or body.get("task")!={"split":split,"index":index}:
        raise ValueError("Receipt is for another task")
    value=body["score"]["reward"]
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1: raise ValueError("Invalid receipt reward")
    return float(value), body["score"]["success"] is True, body


def _package(destination, tier="easy"):
    target=destination/"airline_recovery"/"live"
    target.mkdir(parents=True,exist_ok=True)
    (target.parent/"__init__.py").write_text('"""Airline Recovery Env live Harbor runtime."""\n')
    for name in CORE+HARD_CORE:
        source=Path(__file__).with_name(name)
        if source.is_file():
            shutil.copy2(source,target/name)
        elif name in CORE or tier=="hard":
            raise FileNotFoundError(f"{name} is required for {tier} tasks and is missing from this build")


def case_for_tier(split, index, tier="easy"):
    """The trusted case behind one task; hard cases carry ``level`` and ``budget``."""
    from .scenarios import case_for
    if check_tier(tier)=="easy":
        return case_for(split,index)
    try:
        return case_for(split,index,tier=tier)
    except TypeError as exc:
        raise ValueError("the hard tier is not available in this build") from exc


def _difficulty(split, index, tier):
    if tier=="hard":
        return f"hard-{case_for_tier(split,index,tier).level}"
    return "hard" if any(c.split==split and c.index==index and c.delayed for c in CASES) else "medium"


def build(output="live-tasks",seed=None,tier="easy"):
    """Write one task per case. ``seed=None`` lets each sidecar draw a fresh seed.

    Easy tasks live under ``<output>/<split>/<id>/`` as before; hard tasks under
    ``<output>/hard/<split>/<id>/``. ``tier="all"`` writes both sets.
    """
    root=Path(output)
    tiers=TIERS if tier=="all" else (check_tier(tier),)
    tasks=[task for current in tiers for task in _build_tier(root,seed,current)]
    (root/"manifest.json").write_text(json.dumps({"version":__version__,"tasks":tasks},indent=2))
    return {"tasks":len(tasks),"output":str(root)}


def _build_tier(root,seed,tier):
    tasks=[]
    module,policy_name,solution_files=SOLUTION[tier]
    for split,items in manifest_for(tier).items():
        for item in items:
            path=(root if tier=="easy" else root/tier)/split/item["id"]
            environment=path/"environment"
            # Docker BuildKit reuses a cached build context when the folder name, file path, size and
            # mtime match, and archives give every file the same mtime. Task-unique names keep one
            # task's spec and key from being built into another task's sidecar or verifier.
            world=environment/f"world-{item['id']}"
            tests=path/"tests"
            solution=path/"solution"
            if path.exists(): shutil.rmtree(path)  # generated output; never mix task versions
            for folder in (world,tests,solution): folder.mkdir(parents=True,exist_ok=True)
            spec={"split":split,"index":item["index"],"tier":tier}
            if seed is not None: spec["seed"]=seed
            _package(world,tier)
            # Shared only by the sidecar and verifier images, never the agent image.
            key=secrets.token_hex(32)
            (world/"task_spec.json").write_text(json.dumps(spec))
            (world/"receipt.key").write_text(key)
            # Harbor fixes the verifier folder name, so the files themselves carry the task id.
            (tests/f"{item['id']}.spec.json").write_text(json.dumps(spec))
            (tests/f"{item['id']}.receipt.key").write_text(key)
            (environment/"episode.json").write_text(json.dumps({"schema_version":3,"actions":[]}))
            (environment/"control.py").write_text(CONTROL)
            (environment/"Dockerfile").write_text(f"FROM {IMAGE}\nWORKDIR /app\nCOPY control.py episode.json /app/\n")
            (world/"Dockerfile").write_text(f"FROM {IMAGE}\nWORKDIR /app\nCOPY airline_recovery /app/airline_recovery\nCOPY task_spec.json receipt.key /app/\nCMD [\"python\",\"-m\",\"airline_recovery.live.bridge\",\"--spec\",\"/app/task_spec.json\",\"--key\",\"/app/receipt.key\"]\n")
            (environment/"docker-compose.yaml").write_text(f'''services:
  main:
    depends_on:
      world:
        condition: service_healthy
  world:
    build:
      context: ./{world.name}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=2)"]
      interval: 1s
      timeout: 3s
      retries: 30
    mem_limit: 512m
''')
            (tests/"Dockerfile").write_text(f"FROM {IMAGE}\nWORKDIR /tests\nCOPY . /tests/\n")
            (tests/"grade.py").write_text(GRADE)
            (tests/"test.sh").write_text('#!/bin/sh\nmkdir -p /logs/verifier\nprintf \'{"reward":0,"success":0}\\n\' > /logs/verifier/reward.json\n/usr/local/bin/python -I /tests/grade.py\n')
            (solution/"solve.sh").write_text('#!/bin/sh\nset -eu\n/usr/local/bin/python /solution/solve.py\n')
            (solution/"solve.py").write_text(ORACLE.format(module=module,policy=policy_name))
            for name in solution_files:
                shutil.copy2(Path(__file__).with_name(name),solution/name)
            budget=None if tier=="easy" else case_for_tier(split,item["index"],tier).budget
            (path/"instruction.md").write_text(instruction(tier=tier,budget=budget))
            (path/"task.toml").write_text(f'''schema_version = "1.4"
artifacts = ["/app/episode.json"]
[task]
name = "airline-recovery/{item['id']}"
version = "{__version__}"
description = "Recover live airline transactions without corrupting bookings, charges or seat capacity"
[metadata]
difficulty = "{_difficulty(split,item['index'],tier)}"
category = "sre"
tags = ["transactions", "incident-recovery", "stateful", "http", "{split}"{', "hard"' if tier=='hard' else ''}]
[agent]
timeout_sec = 900.0
[verifier]
timeout_sec = 120.0
environment_mode = "separate"
[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 512
network_mode = "public"
''')
            # Relative to the output root, so an absolute --output never leaks a local path into the manifest.
            tasks.append({"path":path.relative_to(root).as_posix(),**spec})
    return tasks


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",default="live-tasks")
    parser.add_argument("--seed",type=int,default=None,help="pin every sidecar to one seed (debugging); default draws a fresh seed per run")
    parser.add_argument("--tier",default="easy",choices=TIERS+("all",),help="which tier to generate (default: easy)")
    args=parser.parse_args()
    print(json.dumps(build(args.output,args.seed,args.tier),indent=2))
