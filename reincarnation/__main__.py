from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import load, readiness


def main():
    parser = argparse.ArgumentParser(description="Fortnite Reincarnation — Windows screen automation")
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"), help="Profile, screenshots and logs")
    parser.add_argument("--init", action="store_true", help="Create/validate settings without starting automation")
    parser.add_argument("--check", action="store_true", help="Check calibrated profile; returns 2 if incomplete")
    parser.add_argument("--classify", type=Path, help="Classify an existing screenshot without sending input")
    args = parser.parse_args()
    root = args.data_dir.resolve()
    try:
        cfg = load(root / "settings.json")
        if args.init:
            print(root / "settings.json")
            return 0
        if args.check:
            from .vision import Recognizer
            Recognizer(cfg["profile"], root)
            problems = readiness(cfg, root)
            print("\n".join(problems) if problems else "Profile ready")
            return 2 if problems else 0
        if args.classify:
            from PIL import Image
            from .vision import Recognizer
            with Image.open(args.classify) as frame:
                print(Recognizer(cfg["profile"], root).classify(frame))
            return 0
        if sys.platform != "win32":
            parser.error("Live mode requires Windows")
        from .windows import dpi_awareness
        dpi_awareness()
        from .gui import App
        App(root).run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
