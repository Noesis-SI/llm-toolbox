# toolbox

A collection of unrelated command-line tools whose user is an LLM agent, not a human. Every tool is
reached through one dispatcher, `tb`, and obeys one output contract, so learning one tool teaches the
calling convention for all of them.

## Using the tools

```
./tb list              # every tool, one line each (JSON)
./tb list esp          # filter by a word in the name, summary or tags
./tb TOOL --help       # subcommands, flags, examples, output fields, error codes
./tb TOOL ARGS...      # run it
```

`tb` can be called from any directory by its absolute path, or symlinked onto `PATH`. Relative paths
passed to a tool resolve against the caller's working directory.

## The contract

Every tool, in every language, must behave this way. `./tb check` verifies the mechanical parts.

| Channel   | Rule |
|-----------|------|
| stdout    | Exactly one JSON object and nothing else. Success: `{"ok": true, "data": {...}}`. Failure: `{"ok": false, "error": {"code", "message", "hint"}}`, optionally with `error.details`. |
| stderr    | Progress and logs only. The result must be fully understandable from stdout alone. |
| exit code | `0` success, `1` the tool ran and failed, `2` it was called incorrectly. |
| `--help`  | The one exception to JSON: plain text on stdout, exit `0`. |

Beyond the mechanics:

- **Errors say what to do next.** `error.code` is a stable snake_case identifier. `error.hint` names the
  concrete next action (a flag to add, a command to run), not a restatement of the failure.
- **Never interactive.** No prompts, no pagers, no confirmation questions. Everything is a flag or an
  environment variable.
- **Always terminates.** Every network or wait operation has a default timeout, exposed as a flag.
- **Bounded output.** Never print unbounded data. Truncate, set `"truncated": true`, and offer a flag to
  page or narrow.
- **`--help` is the documentation.** It must contain copy-pasteable examples, the fields of `data` for
  each subcommand, and the error codes. An agent should not need to read source to use a tool.
- **Look before you leap.** Split read-only subcommands (`discover`, `list`, `status`) from ones that
  change something, and give anything destructive or hard to undo a `--dry-run`.
- **Honest results.** `ok: true` means the thing verifiably happened. Partial or unverified outcomes get
  an explicit field (e.g. `back_online: false`) and a `warning`, not a silent success.

## Layout

```
tb                     dispatcher (stdlib-only uv script)
lib/tb_contract/       Python helper implementing the contract
tools/<name>/
  tool.toml            manifest read by the dispatcher
  ...                  the tool itself, fully self-contained
```

Tools share nothing except the contract. Each has its own dependencies and may be written in any
language.

## Adding a tool

1. Create `tools/<name>/tool.toml`:

   ```toml
   name = "<name>"                       # must equal the directory name
   summary = "One sentence, <= 140 chars, saying what it does and on what."
   tags = ["keywords", "for", "tb list QUERY"]
   command = ["uv", "run", "--quiet", "--project", "{tool_dir}", "<name>"]
   ```

   `command` is the argv the dispatcher execs, with the caller's arguments appended. `{tool_dir}` expands
   to the tool's absolute directory. The command must not change the working directory.

2. For a Python tool, make `tools/<name>` a uv project that depends on `tb-contract`
   (see `tools/esp-ota/pyproject.toml`) and use its helpers:

   - `ContractArgumentParser` - an `argparse` parser that reports usage errors as contract JSON with
     exit `2`. Set `prog="tb <name>"` and put examples in `epilog`.
   - `ToolError(code, message, hint, details=None)` - raise for any expected failure.
   - `progress(message)` - write a progress line to stderr.
   - `run(function)` - call the function, print its return value as `data`, and convert exceptions into
     error objects.

   Tools in other languages implement the contract table above directly.

3. Run `./tb check <name>` and fix anything it reports.

`tools/esp-ota` is the reference implementation.
