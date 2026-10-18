"""Datasette configuration shipped with the package (see DESIGN.md section 8).

Installed versions: datasette 0.65 and datasette-dashboards 0.8. In these versions
everything (title, table facets, canned queries and the dashboards, which live under
``plugins``) is read from a single metadata file, so ``config_path()`` and
``metadata_path()`` point to the same file. Run Datasette with::

    datasette <expenses.db> --metadata <metadata_path()> --setting max_returned_rows 5000
"""

from __future__ import annotations

from pathlib import Path

DATABASE_NAME = "expenses"
_METADATA = Path(__file__).resolve().parent / "metadata.yaml"

# Dashboard charts fetch their data through the JSON API, which truncates at 1000 rows.
SETTINGS = {"max_returned_rows": "5000"}


def metadata_path() -> Path:
    return _METADATA


def config_path() -> Path:
    return _METADATA


def datasette_args(db_path: str | Path, port: int = 8001) -> list[str]:
    """Arguments for ``datasette serve`` (without the leading ``datasette``)."""
    args = [
        "serve", str(db_path),
        "--metadata", str(metadata_path()),
        "--host", "127.0.0.1", "--port", str(port),
    ]
    for key, value in SETTINGS.items():
        args += ["--setting", key, value]
    return args
