"""Client wrapper around the sandbox subprocess runner."""

import pandas as pd

from backend.config import settings
from backend.sandbox import runner


class CodeRunner:
    def __init__(self, timeout_s: float | None = None, cap: int | None = None):
        self.timeout_s = timeout_s or settings.sandbox_timeout_s
        self.cap = cap or settings.sandbox_max_result_chars

    def run(
        self,
        code: str,
        df: pd.DataFrame | None = None,
        frames: dict[str, pd.DataFrame] | None = None,
        primary: str | None = None,
    ) -> dict:
        """Execute model-authored pandas code against one or more datasets.

        Never raises for user-code problems; failures come back as
        {ok: False, error} so the agent can self-repair.
        """
        if not code or not code.strip():
            return {
                "ok": False,
                "error": "empty code",
                "stdout": "",
                "result_repr": "",
                "result_type": None,
            }
        return runner.run_code(
            code,
            df=df,
            timeout_s=self.timeout_s,
            max_result_chars=self.cap,
            frames=frames,
            primary=primary,
        )
