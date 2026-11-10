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

## Browser (`browse`)

Headless Chromium (Playwright) for pages that need JavaScript, forms or clicks. It renders a page's
visible text, takes a screenshot, or runs fill/click/wait_for/press/extract steps. It stops and reports
a `blocked` error at a CAPTCHA, bot check or access wall; it does not try to get past one.

```
./tb browse text https://example.com
./tb browse screenshot https://example.com /tmp/page.png
./tb browse run https://site.example/form --steps /tmp/steps.json
```

Setup, once per machine: `cd tools/browse && uv run --project . playwright install chromium`, then
Chromium's system libraries must be present (see `./tb browse --help`).
