import sys

from oriel.tiling.daemon import run
from oriel.tiling.geometry import list_monitors
from oriel.tiling.persistence import dump_state

if __name__ == "__main__":
    if "--list-monitors" in sys.argv:
        list_monitors()
    elif "--dump-state" in sys.argv:
        dump_state()
    else:
        run()
