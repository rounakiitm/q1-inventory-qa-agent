"""Eval harness: fires the agent at ground-truth Q&A pairs and scores it.

Usage:
    python evals/run_eval.py [--url http://localhost:8000] [--out eval_report.md]
"""

import argparse
import os
import re
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent


def extract_number(text: str) -> float | None:
    matches = re.findall(r"-?\d[\d,]*\.?\d*", text)
    values = []
    for m in matches:
        try:
            values.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return values[-1] if values else None


def check_pair(pair: dict, answer: dict, trace: list[str]) -> tuple[bool, str]:
    check = pair["check"]
    text = answer.get("answer", "")
    kind = check["type"]
    if kind == "numeric":
        got = extract_number(text)
        if got is None:
            return False, "no number found in answer"
        tol = check.get("tol", 1.0)
        expected = float(check["expected"])
        ok = abs(got - expected) <= abs(expected) * tol / 100 + 1e-9
        return ok, f"got {got}, expected {expected} (±{tol}%)"
    if kind == "contains":
        missing = [e for e in check["expected"] if e.lower() not in text.lower()]
        return (not missing), ("missing: " + ", ".join(missing) if missing else "all substrings found")
    if kind == "any_tool":
        ok = check["tool"] in trace
        return ok, f"tool {check['tool']} {'used' if ok else 'NOT used'}"
    return False, f"unknown check type {kind}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("AGENT_URL", "http://localhost:8000"))
    parser.add_argument("--out", default=str(HERE.parent / "eval_report.md"))
    args = parser.parse_args()

    pairs = yaml.safe_load((HERE / "qa_pairs.yaml").read_text())
    results = []
    session = None

    for pair in pairs:
        if not pair.get("follow_up") or session is None:
            session = requests.post(f"{args.url}/sessions", timeout=30).json()["session_id"]
        t0 = time.time()
        resp = requests.post(
            f"{args.url}/sessions/{session}/messages",
            params={"stream": "false"},
            json={"question": pair["q"]},
            timeout=300,
        )
        latency = time.time() - t0
        if resp.status_code != 200:
            results.append({"q": pair["q"], "ok": False, "note": f"HTTP {resp.status_code}", "latency": latency, "tools": []})
            continue
        answer = resp.json()
        trace = [r["tool"] for r in answer.get("tool_runs", [])]
        ok, note = check_pair(pair, answer, trace)
        results.append(
            {
                "q": pair["q"],
                "ok": ok,
                "note": note,
                "latency": latency,
                "tools": sorted(set(trace)),
            }
        )

    passed = sum(1 for r in results if r["ok"])
    lines = [
        "# Eval report",
        "",
        f"**Pass rate: {passed}/{len(results)}**",
        "",
        "| # | Question | Pass | Note | Latency | Tools |",
        "|---|----------|------|------|---------|-------|",
    ]
    for i, (pair, r) in enumerate(zip(pairs, results), 1):
        lines.append(
            f"| {i} | {pair['q'][:60]} | {'✅' if r['ok'] else '❌'} | {r['note']} | {r['latency']:.1f}s | {', '.join(r['tools'])} |"
        )
    report = "\n".join(lines)
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    main()
