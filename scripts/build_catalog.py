"""Build a catalog for any HTTPS location hosting these single-file providers."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="https://example.invalid/lazarr/providers")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / "src/lazarr/bundled"
items = []
for name, kind in [
    ("tmdb", "metadata"),
    ("nyaa", "content"),
    ("rutracker", "content"),
    ("podnapisi", "subtitle"),
    ("opensubtitles", "subtitle"),
]:
    tree = ast.parse((root / f"{name}.py").read_text())
    manifest = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProviderManifest"
    )
    fields = {
        keyword.arg: keyword.value.value
        for keyword in manifest.keywords
        if isinstance(keyword.value, ast.Constant)
    }
    items.append(
        {
            "id": name,
            "kind": kind,
            "version": fields["version"],
            "sdk": fields.get("sdk", ">=1,<2"),
            "url": f"{args.base_url.rstrip('/')}/{name}.py",
            "sha256": hashlib.sha256((root / f"{name}.py").read_bytes()).hexdigest(),
        }
    )
(root / "catalog.json").write_text(json.dumps({"schema_version": 1, "plugins": items}, indent=2) + "\n")
