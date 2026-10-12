"""Generate a guarded personal-client APIM policy candidate without changing Azure."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.services.client_user_policy import upgrade_client_user_policy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Current API policy, raw XML")
    parser.add_argument("--output", type=Path, required=True, help="New candidate; must not exist")
    parser.add_argument("--gateway-id", required=True, help="Platform gateway-profile UUID")
    args = parser.parse_args()
    candidate = upgrade_client_user_policy(args.source.read_text(), gateway_id=args.gateway_id)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(candidate)
    print("Created personal-client policy v2 candidate. No Azure resources were modified.")


if __name__ == "__main__":
    main()
