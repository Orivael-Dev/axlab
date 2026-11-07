# AxLab MCP server

An MCP server that gives an agent (Claude Code, Claude Desktop, or any MCP
client) two things: a **kill switch it can pull but not release**, and the
AxLab benchmarks. It uses only the standard library and talks over stdio.

```bash
pip install -e .
python -m axlab.mcp          # or: axlab-mcp
```

## Connect it

**Claude Code** from a clone of this repo: the repo's `.mcp.json` registers
it, and Claude Code asks you to approve it the first time. From anywhere else:

```bash
claude mcp add axlab -- python -m axlab.mcp
```

**Claude Desktop / other clients:** use the command `python -m axlab.mcp`
(or the full path to `axlab-mcp`). If the python you name isn't the one you
installed AxLab into, use that environment's interpreter.

## Tools

| Tool | What it does | Changes anything? |
|---|---|---|
| `axlab_fleet_status` | Effective kill-switch level for the fleet and any scopes you name | No |
| `axlab_fleet_raise` | Raise a scope to L1–L4 with a reason | Yes: it stops agent work |
| `axlab_tracks` | The research tracks in this checkout | No |
| `axlab_bench_slim_fabric` | Slim fabric run with the scripted model (up to 100k agents, 500 live events) | No (temp files only) |
| `axlab_bench_kill_switch` | One kill-switch experiment in quick mode: `footprint`, `overhead`, `propagation`, `throughput`, `halt` | No (temp files only) |

### The kill switch

The levels are those of [`axlab.governance.fleet_level`](../axlab/governance/fleet_level.py):

| Level | Effect on the slim fabric |
|---|---|
| L1 WARNING | flagged in receipts; nothing else changes |
| L2 THROTTLE | one model slot; side effects need a human |
| L3 SUSPEND | no new model calls; runs checkpoint and can resume |
| L4 TERMINATE | in-flight work dropped; runs end and can't resume |

Scopes: `fleet`, `loop:<name>`, `run:<id>`, `fabric:<name>`, `agent:<id>`.
A reader's level is the highest over `fleet` and its own scopes.

**Raise only.** Over MCP a level can only go up, and every raise needs a
reason. It's written to a hash-chained receipt log next to the governance
file, as `by: "mcp:<client name>"`. Lowering is a **signed release**, which
needs the release key and is done by a person on the command line:

```bash
python -m axlab.governance.fleet_level status
python -m axlab.governance.fleet_level release fleet 0 --reason "checked, safe to resume"
```

The release key is `NXL_RELEASE_KEY` (hex), or `~/.nodexloop/release.key`,
which is created on first release.

**Which file.** `NXL_GOVERNANCE_FILE` if it's set, otherwise
`.nodexloop/governance.json` under the server's working directory. That's the
same file the slim fabric runtime, AXIOM loop runs and the NodeXLoop
extension read, so one raise stops all of them. To point the server at a
specific project, set it in the client config:

```json
{ "mcpServers": { "axlab": {
    "command": "python", "args": ["-m", "axlab.mcp"],
    "env": { "NXL_GOVERNANCE_FILE": "/path/to/project/.nodexloop/governance.json" } } } }
```

**What it is not.** It isn't a security boundary against code running as the
same OS user: such code can edit the file or read the key. The guarantee is
that a stop always works and a release is always deliberate and receipted.

### The benchmarks

Each benchmark runs as a subprocess in a fresh temp directory, with the
caller's master key, release key and governance file removed from its
environment. A benchmark can't change the real fleet level, and the real
level doesn't stop a benchmark. No network, no model: both use a scripted or
simulated model. The tool returns the full JSON report and the path to the
file it wrote. The published results in `experiments/*/results/` are never
overwritten.

Tool calls run on worker threads, so a raise is answered even while a
benchmark is still running.

## Protocol

MCP over stdio, newline-delimited JSON-RPC 2.0. Supported protocol versions:
`2025-06-18`, `2025-03-26` and `2024-11-05`; an unknown requested version
gets `2025-06-18`. Tool results carry both a text block and `structuredContent`.
Logs go to stderr.
