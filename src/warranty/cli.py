"""Standard local entry points; private development tools are not required."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .errors import ServiceError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="warranty")
    parser.add_argument("command", choices=("status", "streamlit", "api", "worker", "validate-input"))
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-input":
            from .catalog import Catalog
            if not args.metadata:
                parser.error("validate-input requires --metadata")
            result = Catalog.load(args.metadata, expected_hash=args.sha256).summary()
        elif args.command == "streamlit":
            script = Path(__file__).resolve().parent / "interfaces" / "streamlit_app.py"
            code = subprocess.call([sys.executable, "-m", "streamlit", "run", str(script),
                                    "--server.address", "127.0.0.1", "--server.port", str(args.port or 8501),
                                    "--browser.gatherUsageStats", "false", "--server.headless", "true"], shell=False)
            raise SystemExit(code)
        elif args.command == "api":
            import uvicorn
            uvicorn.run("warranty.interfaces.api:create_app", factory=True, host="127.0.0.1", port=args.port or 8000,
                        access_log=False, proxy_headers=False)
            return
        else:
            from .application import build_application
            app = build_application(metadata_path=args.metadata)
            try:
                result = app.summary() if args.command == "status" else app.worker.run_pending()
            finally:
                app.close()
        print(json.dumps({"data": result, "error": None}, ensure_ascii=True, indent=2))
    except ServiceError as error:
        print(json.dumps({"data": None, "error": error.as_dict()}, ensure_ascii=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
