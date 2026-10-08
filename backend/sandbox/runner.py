"""Sandboxed pandas code execution.

Contract with the model:
  - `df` (the dataset), `pd` and `np` are preloaded in the exec namespace
  - the code must assign its final finding to a variable named `result`
Safety layers:
  1. AST gate before launch: import allowlist, dangerous-builtin blocklist,
     no dunder attribute access.
  2. Process isolation: the code runs in a fresh Python subprocess with a
     temp working directory, a hard timeout, and capped output.
Known limitation (documented in README): a container boundary would be
stronger; the subprocess + AST gate is the pragmatic middle ground for this
assessment and is swappable via the same interface.
"""

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ALLOWED_IMPORTS = {
    "pandas",
    "numpy",
    "math",
    "statistics",
    "datetime",
    "re",
    "json",
    "itertools",
    "collections",
    "openpyxl",  # pandas Excel helper; already a project dependency
}
BANNED_MODULES = {
    "os",
    "sys",
    "socket",
    "subprocess",
    "shutil",
    "pathlib",
    "importlib",
    "ctypes",
    "webbrowser",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "multiprocessing",
    "threading",
    "signal",
    "pickle",
    "shelve",
    "gc",
    "builtins",
}
BANNED_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "__import__",
    "exit",
    "quit",
}

_RUNNER_TEMPLATE = '''
import io, json, contextlib, re
import pandas as pd, numpy as np

with open("meta.json") as f:
    cfg = json.load(f)
frames = {}
for item in cfg.get("frames", []):
    frames[item["name"]] = pd.read_csv(item["file"])
primary = cfg.get("primary")
df = frames.get(primary) if primary in frames else (next(iter(frames.values())) if frames else None)
NS = {"pd": pd, "np": np, "df": df, "dfs": frames, "result": None}
# also expose each frame as a safe attribute-like name if it is a valid identifier
for _n, _f in frames.items():
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", _n):
        NS[_n] = _f
buf = io.StringIO()
error = None
try:
    with contextlib.redirect_stdout(buf):
        exec(compile(cfg["code"], "agent_code", "exec"), NS)
except BaseException as e:  # noqa: BLE001 - reported to the agent for repair
    error = f"{type(e).__name__}: {e}"
result = NS.get("result")
if result is not None and hasattr(result, "item") and not isinstance(result, (pd.DataFrame, pd.Series)):
    try:
        result = result.item()
    except Exception:
        pass
result_repr = ""
if result is not None:
    try:
        if isinstance(result, pd.DataFrame):
            result_repr = result.to_string(max_rows=50)
        elif isinstance(result, pd.Series):
            result_repr = result.to_string()
        else:
            result_repr = repr(result)
    except Exception as e:  # noqa: BLE001
        result_repr = f"<unrepresentable result: {e}>"
out = {
    "ok": error is None,
    "error": error,
    "stdout": buf.getvalue()[-4000:],
    "result_repr": result_repr[: cfg["cap"]],
    "result_type": type(result).__name__ if result is not None else None,
}
print("<<<RESULT>>>" + json.dumps(out, default=str))
'''


class SandboxError(RuntimeError):
    """Raised for sandbox-level failures (timeout, unsafe code)."""


def check_code(code: str) -> None:
    """Raise SandboxError if the code violates the safety policy."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        # syntax errors are repairable, not a safety issue: report via stdout
        raise SandboxError(f"SyntaxError: {exc.msg} (line {exc.lineno})") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    raise SandboxError(
                        f"SafetyError: import {alias.name} is not allowed"
                    )
                if root not in ALLOWED_IMPORTS:
                    raise SandboxError(
                        f"SafetyError: import {alias.name} is not in the allowlist"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                raise SandboxError(
                    f"SafetyError: from {node.module} import ... is not allowed"
                )
            if root and root not in ALLOWED_IMPORTS:
                raise SandboxError(
                    f"SafetyError: from {node.module} import ... is not in the allowlist"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in BANNED_CALLS:
                raise SandboxError(f"SafetyError: {name}() is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SandboxError("SafetyError: dunder attribute access is not allowed")


def run_code(
    code: str,
    df: pd.DataFrame | None = None,
    timeout_s: float = 15.0,
    max_result_chars: int = 10000,
    frames: dict[str, pd.DataFrame] | None = None,
    primary: str | None = None,
) -> dict:
    """Execute `code` against one or more dataframes in a fresh subprocess.

    Prefer `frames` + `primary`. A bare `df` is still accepted for back-compat.
    Never raises for user-code problems - failures are returned as {ok: False, error}.
    """
    try:
        check_code(code)
    except SandboxError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "stdout": "",
            "result_repr": "",
            "result_type": None,
        }

    frame_map: dict[str, pd.DataFrame] = dict(frames or {})
    if not frame_map and df is not None:
        frame_map = {"df": df}
        primary = primary or "df"
    if not frame_map:
        return {
            "ok": False,
            "error": "no dataset attached to this session — attach a workbook first",
            "stdout": "",
            "result_repr": "",
            "result_type": None,
        }
    if primary not in frame_map:
        primary = next(iter(frame_map))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frame_meta = []
        for i, (name, frame) in enumerate(frame_map.items()):
            fname = f"data_{i}.csv"
            frame.to_csv(tmp / fname, index=False)
            frame_meta.append({"name": name, "file": fname})
        (tmp / "meta.json").write_text(
            json.dumps(
                {
                    "code": code,
                    "cap": max_result_chars,
                    "frames": frame_meta,
                    "primary": primary,
                }
            ),
            encoding="utf-8",
        )
        (tmp / "runner.py").write_text(_RUNNER_TEMPLATE, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "runner.py"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"SandboxTimeoutError: execution exceeded {timeout_s}s"
                " and was terminated",
                "stdout": "",
                "result_repr": "",
                "result_type": None,
            }

        out = proc.stdout or ""
        marker = out.rfind("<<<RESULT>>>")
        if marker == -1:
            stderr_tail = (proc.stderr or "").strip()[-2000:]
            return {
                "ok": False,
                "error": stderr_tail or "sandbox produced no result marker",
                "stdout": out[-2000:],
                "result_repr": "",
                "result_type": None,
            }
        return json.loads(out[marker + len("<<<RESULT>>>") :])
