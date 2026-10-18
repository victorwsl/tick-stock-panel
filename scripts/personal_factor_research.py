#!/usr/bin/env python3
"""Import an immutable factor run and independently verify it inside Panel."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from app.config import settings
    from app.services.personal_factor_research import import_study, verify

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short-run", type=Path, required=True)
    parser.add_argument("--long-run", type=Path, required=True)
    parser.add_argument("--id", required=True)
    args = parser.parse_args()
    if not settings.personal_local_only:
        parser.error("本入口要求PERSONAL_LOCAL_ONLY=true")
    import_study(settings.data_dir, args.short_run, args.long_run, args.id)
    print(json.dumps(verify(settings.data_dir, args.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
