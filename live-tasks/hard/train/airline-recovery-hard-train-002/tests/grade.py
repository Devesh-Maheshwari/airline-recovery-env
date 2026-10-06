import hashlib,hmac,json,math,os,stat
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
