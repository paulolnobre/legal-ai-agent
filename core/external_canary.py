"""Run one safe live probe for each configured external research dependency."""
import asyncio
import json

from core.services.external_capabilities import run_external_canaries


def main() -> int:
    report = asyncio.run(run_external_canaries())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
