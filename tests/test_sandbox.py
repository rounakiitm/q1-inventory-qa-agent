import pandas as pd

from backend.sandbox import runner
from backend.sandbox.runner import SandboxError


def test_simple_result():
    df = pd.DataFrame({"a": [1, 2, 3], "b": [10, 20, 30]})
    out = runner.run_code("result = df['a'].sum()", df, 10, 1000)
    assert out["ok"] is True
    assert out["result_repr"] == "6"


def test_dataframe_result():
    df = pd.DataFrame({"a": [1, 2, 3]})
    out = runner.run_code("result = df[df['a'] > 1]", df, 10, 1000)
    assert out["ok"] is True
    assert out["result_type"] == "DataFrame"
    assert "2" in out["result_repr"]


def test_banned_import_rejected():
    df = pd.DataFrame({"a": [1]})
    out = runner.run_code("import os\nresult = os.getcwd()", df, 10, 1000)
    assert out["ok"] is False
    assert "not allowed" in out["error"]


def test_banned_builtin_rejected():
    df = pd.DataFrame({"a": [1]})
    out = runner.run_code("result = open('x.txt')", df, 10, 1000)
    assert out["ok"] is False
    assert "not allowed" in out["error"]


def test_dunder_rejected():
    df = pd.DataFrame({"a": [1]})
    out = runner.run_code("result = df.__class__", df, 10, 1000)
    assert out["ok"] is False
    assert "dunder" in out["error"]


def test_runtime_error_is_repairable():
    df = pd.DataFrame({"a": [1, 2]})
    out = runner.run_code("result = df['missing_col'].sum()", df, 10, 1000)
    assert out["ok"] is False
    assert "KeyError" in out["error"]


def test_timeout_kills_infinite_loop():
    df = pd.DataFrame({"a": [1]})
    out = runner.run_code("while True:\n    pass\nresult = 1", df, 2, 1000)
    assert out["ok"] is False
    assert "terminated" in out["error"]


def test_syntax_error_reported():
    raised = False
    try:
        runner.check_code("result = (")
    except SandboxError:
        raised = True
    assert raised
