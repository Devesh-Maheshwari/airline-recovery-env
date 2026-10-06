# Trust boundary

The native OpenEnv server owns worker processes, scenario selection, episode
databases and scoring. A remote agent receives typed observations and can invoke
only the published tools. The bounded SQL interface uses a read-only connection,
an authorizer and a computation budget; it cannot write or attach databases.

This is a local research environment. Authentication, tenant quotas, hardened
container isolation and network access controls for a public multi-user service
are not implemented here. Bind local development to `127.0.0.1`; put deployment
controls in front of a server exposed to others. Never attach production data.

Python policy plugins execute in the evaluator's process and are trusted code.
Use the OpenEnv client/server boundary for untrusted policies. The native `/web`
playground is a shared debugging episode; independent training clients should
use WebSocket sessions, which each own an isolated stack.

The project has not undergone an external security audit. Report a suspected
vulnerability privately to the repository maintainers; do not include secret
values or third-party data in a public report.
