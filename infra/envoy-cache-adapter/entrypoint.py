from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = Path("/tmp/turnstile-envoy/envoy.yaml")


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing required setting: {name}")
    return value


def render_config() -> None:
    completion = os.environ.get("BUDGET_COMPLETION_ENABLED", "false").lower()
    if completion not in {"true", "false"}:
        raise RuntimeError("BUDGET_COMPLETION_ENABLED must be true or false")
    if completion == "true":
        from turnstile_core.integrations.budget_completion import BudgetCompletionClient

        BudgetCompletionClient(required("LEDGER_TABLE_ENDPOINT"), required("LEDGER_TABLE_NAME"),
                               lambda: "")
    adapter_key = required("ADAPTER_SHARED_KEY")
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", adapter_key):
        raise RuntimeError("ADAPTER_SHARED_KEY has an invalid value")
    template = (ROOT / "envoy.yaml.template").read_text(encoding="utf-8")
    CONFIG.write_text(
        template.replace("__ADAPTER_SHARED_KEY__", adapter_key),
        encoding="utf-8",
    )


def main() -> None:
    render_config()
    shipper = subprocess.Popen(["python3", str(ROOT / "shipper.py")])
    envoy = subprocess.Popen(
        [
            "envoy",
            "-c",
            str(CONFIG),
            "--log-level",
            "info",
            "--file-flush-interval-msec",
            "100",
        ]
    )

    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        envoy.terminate()
        shipper.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while envoy.poll() is None:
            if shipper.poll() is not None and not stopping:
                shipper = subprocess.Popen(["python3", str(ROOT / "shipper.py")])
            time.sleep(0.5)
    finally:
        if shipper.poll() is None:
            shipper.terminate()
        shipper.wait(timeout=10)
    raise SystemExit(envoy.returncode or 0)


if __name__ == "__main__":
    main()