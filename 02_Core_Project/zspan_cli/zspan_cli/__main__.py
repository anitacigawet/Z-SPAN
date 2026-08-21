"""`python -m zspan_cli` entry point — the run-from-clone invocation form."""
import sys

from zspan_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
