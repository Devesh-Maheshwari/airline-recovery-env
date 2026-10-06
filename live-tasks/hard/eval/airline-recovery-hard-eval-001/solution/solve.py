import sys
sys.path.insert(0,"/app")
from control import call
from oracle import OraclePolicy
policy=OraclePolicy()
transition=call()
while not (transition["terminated"] or transition["truncated"]):
    transition=call(policy(transition["observation"]))
print(transition["reward"])
