#!/bin/sh
mkdir -p /logs/verifier
printf '{"reward":0,"success":0}\n' > /logs/verifier/reward.json
/usr/local/bin/python -I /tests/grade.py
