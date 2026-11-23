"""Windows entry point. Avoid writing import caches during previews."""
import sys

sys.dont_write_bytecode = True

if sys.platform != "win32" or sys.version_info < (3, 13):
    sys.exit("File Organizer requires Windows and Python 3.13 or newer.")

from organizer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
