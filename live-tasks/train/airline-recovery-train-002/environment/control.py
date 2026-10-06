import json,os,sys,urllib.error,urllib.request
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
