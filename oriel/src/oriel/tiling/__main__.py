import sys

from oriel.tiling.daemon import run
from oriel.tiling.geometry import list_monitors

if __name__ == "__main__":
    if "--list-monitors" in sys.argv:
        list_monitors()
    else:
        run()
