"""Hand gestures, spoken confirmation, and local Chinese conversation."""
# Optional project-local wheels; a normal requirements install takes precedence.
import sys
from .paths import DATA

_runtime = DATA / "python"
if _runtime.is_dir():
    sys.path.append(str(_runtime))
