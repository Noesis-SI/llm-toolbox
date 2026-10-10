# LLM Toolbox

A collection of command-line tools built to be used by LLM agents. Every tool runs through one
dispatcher, `tb`, and follows the same contract: one JSON object on stdout, errors that say what to do
next, and no interactive prompts.

## Usage

```
./tb list            # list the tools
./tb TOOL --help     # how to use one
./tb TOOL ARGS...    # run it
```

Requires [uv](https://docs.astral.sh/uv/).
