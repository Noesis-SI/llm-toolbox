import argparse
import json
import sys
from typing import Any, Callable, NoReturn

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _emit(payload: dict[str, Any], exit_code: int) -> NoReturn:
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def _error_payload(code: str, message: str, hint: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "hint": hint}
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


class ContractArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        _emit(
            _error_payload("usage_error", message, f"Run `{self.prog} --help` for valid arguments and examples."),
            EXIT_USAGE,
        )


def run(command: Callable[[], Any]) -> NoReturn:
    try:
        data = command()
    except ToolError as error:
        _emit(_error_payload(error.code, error.message, error.hint, error.details), EXIT_FAILURE)
    except Exception as error:
        _emit(
            _error_payload(
                "internal_error",
                f"{type(error).__name__}: {error}",
                "This is a bug in the tool, not in how it was called. Report it rather than retrying.",
            ),
            EXIT_FAILURE,
        )
    _emit({"ok": True, "data": data}, EXIT_OK)
