"""Fat-Fish-Assistant public command-line management entrypoint."""
from pathlib import Path
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from fat_fish_management.controller import main

if __name__=='__main__':raise SystemExit(main())
