"""Harbor-only tool bridge. World state stays in a separate container.

The bridge owns the single authoritative episode. When it ends, the bridge
issues an HMAC-signed receipt of the outcome; the verifier trusts only that.
"""
import argparse
import hashlib
import hmac
import json
import secrets
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .environment import LiveAirlineEnv


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sign(key, body):
    return hmac.new(key, canonical(body), hashlib.sha256).hexdigest()


RECEIPT_SCHEMA = {"easy":3,"hard":4}


def serve(spec_path, key_path, host="0.0.0.0", port=8081, trace_path=None):
    """``trace_path``, when given, receives one JSON line per request: the reset
    observation first, then every action with the full reply or the rejection."""
    spec = json.loads(Path(spec_path).read_text())
    key = Path(key_path).read_bytes().strip()
    tier = spec.get("tier","easy")
    if tier not in RECEIPT_SCHEMA:
        raise ValueError(f"Unknown tier in task spec: {tier!r}")
    # A fresh seed per container start unless the task pins one for debugging.
    seed = spec["seed"] if "seed" in spec else secrets.randbelow(2**31)
    options = {"split":spec["split"],"index":spec["index"]}
    if tier != "easy":
        options["tier"] = tier
    # The easy budget is 48; a hard episode takes its budget from the case unless the
    # spec raises it (the budget ablation). The environment rejects a budget below the case's.
    max_steps = spec.get("max_steps", 48 if tier == "easy" else None)
    trace = open(trace_path, "a") if trace_path else None
    def record(entry):
        if trace:
            trace.write(json.dumps(entry, allow_nan=False) + "\n")
            trace.flush()
    with LiveAirlineEnv(max_steps=max_steps) as env:
        observation,info = env.reset(seed=seed,options=options)
        current = {"observation":observation,"reward":0,"terminated":False,"truncated":False,"info":info}
        actions = []
        record({"step":0,"kind":"reset","seed":seed,**current})
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def respond(self,code,payload):
                data=json.dumps(payload,allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                self.respond(200,{"ready":True} if self.path=="/health" else current) if self.path in {"/health","/observation"} else self.respond(404,{"error":"Unknown path"})
            def do_POST(self):
                nonlocal current
                if self.path != "/step":
                    return self.respond(404,{"error":"Unknown path"})
                try:
                    length=int(self.headers.get("Content-Length","0"))
                    if not 0 < length <= 65536:
                        raise ValueError("Request must be 1..65536 bytes")
                    action=json.loads(self.rfile.read(length),parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                    # 1e999 parses as inf; reject it before the step, or the receipt could never be signed.
                    canonical(action)
                    obs,reward,terminated,truncated,info=env.step(action)
                    actions.append(action)
                    current={"observation":obs,"reward":reward,"terminated":terminated,"truncated":truncated,"info":info}
                    record({"step":len(actions),"kind":"action","action":action,**current})
                    if terminated or truncated:
                        body={"schema_version":RECEIPT_SCHEMA[tier],"task":{"split":spec["split"],"index":spec["index"]},
                              "tier":tier,"seed":seed,"steps":len(actions),"actions_sha256":hashlib.sha256(canonical(actions)).hexdigest(),
                              "terminated":terminated,"truncated":truncated,"score":info["score"]}
                        current["receipt"]={"body":body,"signature":sign(key,body)}
                    self.respond(200,current)
                except (ValueError,TypeError,RuntimeError) as error:
                    record({"step":len(actions),"kind":"rejected","error":str(error)})
                    self.respond(400,{"error":str(error)})
        server=HTTPServer((host,port),Handler)
        if threading.current_thread() is threading.main_thread():
            # SIGTERM (how runners stop the bridge) unwinds through the with-block, so the episode's
            # workers and temporary directory are cleaned up.
            signal.signal(signal.SIGTERM,lambda *_: sys.exit(0))
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if trace:
                trace.close()


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--spec",required=True)
    parser.add_argument("--key",required=True)
    parser.add_argument("--host",default="0.0.0.0")
    parser.add_argument("--port",type=int,default=8081)
    parser.add_argument("--trace",help="append a JSON line per request (reset, actions, rejections) to this file")
    args=parser.parse_args()
    serve(args.spec,args.key,host=args.host,port=args.port,trace_path=args.trace)
