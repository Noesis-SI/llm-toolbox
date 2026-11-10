import argparse
import json
from pathlib import Path

from tb_contract import ContractArgumentParser, ToolError, run

from browse import core

EPILOG = """examples:
  tb browse text https://example.com
  tb browse text https://site.example/app --wait-until networkidle --timeout 60
  tb browse screenshot https://example.com /tmp/page.png
  tb browse run https://site.example/form --steps /tmp/steps.json

steps file: a JSON list. Actions (selectors are CSS, or Playwright selectors such as text=Sign in):
  {"action":"fill","selector":"#q","value":"text"}
  {"action":"click","selector":"button[type=submit]"}
  {"action":"wait_for","selector":"#results"}
  {"action":"press","key":"Enter","selector":"#q"}     (selector optional)
  {"action":"extract","selector":".result"}           (returns the matching texts)

data fields:
  text:       url, final_url, status, title, text (visible body text, cut at --max-chars), truncated.
  run:        url, final_url, status, steps_done, extracted[{selector, texts}], text, truncated.
  screenshot: url, final_url, status, path.

error codes:
  blocked            a CAPTCHA, bot check or access wall was detected. The tool stops; it does not bypass.
  browser_missing    Chromium is not installed; run playwright install chromium.
  browser_missing_libs  system libraries are missing; the operator must apt install them.
  timeout, navigation_failed, step_timeout, bad_steps, usage_error, browser_error.

Dynamic pages: content that appears after load needs --settle SECONDS (default 1), or use a
run with a wait_for step on the element you need.

Politeness: one page load per call, no retries, no crawling. Do not loop on a site.
"""


def _common(p):
    p.add_argument("--timeout", type=float, default=30, help="seconds per page load or step (default 30)")
    p.add_argument("--user-agent", default=None, help="override the User-Agent header (default: Chromium's own)")
    p.add_argument("--wait-until", default="load", choices=core.WAIT_UNTIL,
                   help="when navigation counts as done (default load)")
    p.add_argument("--settle", type=float, default=1.0,
                   help="extra seconds after load so page scripts can render (default 1; 0 to skip)")


def _parser():
    p = ContractArgumentParser(prog="tb browse", description="Render a page in headless Chromium.",
                               epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=ContractArgumentParser)
    t = sub.add_parser("text", help="return the rendered visible text of a URL", epilog=EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    t.add_argument("url")
    t.add_argument("--max-chars", type=int, default=20000)
    _common(t)
    s = sub.add_parser("screenshot", help="save a PNG screenshot of a URL", epilog=EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("url")
    s.add_argument("out", help="output PNG path")
    s.add_argument("--viewport-only", action="store_true", help="capture only the visible area")
    _common(s)
    r = sub.add_parser("run", help="run fill/click/wait_for/press/extract steps, then return the text", epilog=EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("url")
    r.add_argument("--steps", required=True, help="path to a JSON file with the list of steps")
    r.add_argument("--max-chars", type=int, default=20000)
    _common(r)
    return p


def main() -> None:
    args = _parser().parse_args()
    opts = {"timeout": args.timeout, "user_agent": args.user_agent, "wait_until": args.wait_until,
            "settle": args.settle}

    def command():
        try:
            if args.cmd == "text":
                return core.get_text(args.url, max_chars=args.max_chars, **opts)
            if args.cmd == "screenshot":
                return core.screenshot(args.url, args.out, full_page=not args.viewport_only, **opts)
            steps = json.loads(Path(args.steps).read_text())
            return core.run_steps(args.url, steps, max_chars=args.max_chars, **opts)
        except core.BrowseError as error:
            raise ToolError(error.code, error.message, error.hint, error.details) from error
        except FileNotFoundError as error:
            raise ToolError("file_not_found", str(error), "Pass an existing file path.") from error
        except json.JSONDecodeError as error:
            raise ToolError("bad_steps", f"steps file is not valid JSON: {error}", "Fix the JSON and retry.") from error

    run(command)


if __name__ == "__main__":
    main()
