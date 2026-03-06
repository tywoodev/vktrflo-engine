"""
comfy_extras/web_builtins.py
Register built-in web extensions that are part of this local fork
but live outside the custom_nodes mechanism.
"""
from pathlib import Path
import nodes

_ROOT = Path(__file__).parent.parent / "web_builtins"

_BUILTINS = {
    "block-space": _ROOT / "block_space",
}


def register():
    for name, path in _BUILTINS.items():
        if path.exists():
            nodes.EXTENSION_WEB_DIRS[name] = str(path)
