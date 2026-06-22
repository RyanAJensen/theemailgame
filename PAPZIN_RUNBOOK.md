# Papzin Email Game Competition Runbook

Agent name:
- `letlhogonolo_fanampe`

## 1) Verify access first

Run the preflight helper from the repo root:

```bash
bash preflight_papzin_agent.sh
```

If you want to verify the model gateway before a real run, add the optional key check:

```bash
bash preflight_papzin_agent.sh --check-key
```

Success criteria:
- `.env.local` exists
- `EMAIL_GAME_AGENT_NAME` is exactly `letlhogonolo_fanampe`
- `EMAIL_GAME_SERVER` is set
- `my_agent.py` compiles
- the allowed model verifier passes if you asked for it

## 2) Confirm the agent name

The competition name must be exactly:

```text
letlhogonolo_fanampe
```

Do not change the spelling, underscores, or casing.

## 3) Run the agent

Use the launch helper from the repo root:

```bash
bash run_papzin_agent.sh
```

That helper runs:

```bash
./.venv/bin/python scripts/run_custom_agent.py "$EMAIL_GAME_AGENT_NAME" --module my_agent.py --server "$EMAIL_GAME_SERVER"
```

## 4) Logs to watch

Watch for these startup lines:
- the agent loading `my_agent.py`
- the model and endpoint banner
- queue position updates
- moderator messages for each round
- signature request / submission logs
- any auth or connection failure from startup

Useful signals:
- `Joined matchmaking queue`
- `connected, in the ladder queue, waiting for a game`
- `Round ...`
- `Submitted received signature`
- `Signed request from ...`

## 5) Stop safely

Stop the process with `Ctrl+C`.

That should exit the agent cleanly without restarting anything.

## 6) What not to do

Avoid these because they waste the competition budget:
- do not run repeated live restarts
- do not keep relaunching after auth failures without fixing the env
- do not change the agent name away from `letlhogonolo_fanampe`
- do not print or paste the API key
- do not edit `.env.local` during a live run unless you are intentionally fixing configuration
- do not run extra live smoke tests unless you really need them

## 7) Quick troubleshooting

### Queue timeout
Possible causes:
- the server is temporarily busy
- the server URL is wrong
- the agent never reached the queue

What to check:
- confirm `EMAIL_GAME_SERVER` is set in `.env.local`
- rerun the preflight helper
- try the launch helper once more after verifying the server URL

### Connection timeout
Possible causes:
- the server URL is unreachable
- the network path is down
- the host is temporarily unavailable

What to check:
- confirm `EMAIL_GAME_SERVER` is correct
- verify the local environment and network
- retry once after the issue is corrected

### Auth failure
Possible causes:
- the key does not match the issued competition identity
- the agent name is not exactly `letlhogonolo_fanampe`
- the gateway is unavailable or misconfigured

What to check:
- confirm `EMAIL_GAME_AGENT_NAME=letlhogonolo_fanampe`
- rerun `bash preflight_papzin_agent.sh --check-key`
- confirm the launch helper is sourcing the local `.env.local`

## 8) Recommended order

1. `bash preflight_papzin_agent.sh`
2. `bash run_papzin_agent.sh`
3. Watch the logs and let the agent run
4. Stop with `Ctrl+C` when you are done
