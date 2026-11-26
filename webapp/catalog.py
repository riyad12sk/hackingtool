"""Read-only adapter for the real HackingTool registry.

The web app deliberately exposes metadata only. It never imports or invokes a
Tool's install/run callbacks and never returns command strings to the browser.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _from_engine() -> list[dict[str, Any]]:
    # This is the same engine-backed source used by the upstream Textual UI.
    from hackingtool.tui.data import _engine

    collections, definitions = _engine()
    rows: list[dict[str, Any]] = []
    for index, collection in enumerate(collections):
        definition = definitions[index] if index < len(definitions) else (
            getattr(collection, "TITLE", "Tools"), "•", "Tools"
        )
        category_title, icon, label = definition
        for tool in getattr(collection, "TOOLS", []) or []:
            title = getattr(tool, "TITLE", "")
            if not title:
                continue
            rows.append({
                "title": str(title),
                "description": str(getattr(tool, "DESCRIPTION", "") or ""),
                "category": str(category_title),
                "category_label": str(label),
                "icon": str(icon or "•").strip(),
                "project_url": str(getattr(tool, "PROJECT_URL", "") or ""),
                "kind": str(getattr(tool, "KIND", "install") or "install"),
                "tags": [str(x) for x in (getattr(tool, "TAGS", []) or [])],
                "lab_safe_notes": str(getattr(tool, "LAB_SAFE_NOTES", "") or ""),
                "archived": bool(getattr(tool, "ARCHIVED", False)),
                "archived_reason": str(getattr(tool, "ARCHIVED_REASON", "") or ""),
            })
    return rows


def _from_yaml() -> list[dict[str, Any]]:
    """Fallback for environments where importing the CLI engine is unavailable."""
    import yaml

    try:
        import hackingtool.registry as registry
        catalog_dir = registry.CATALOG_DIR
    except Exception:
        catalog_dir = Path(__file__).resolve().parents[1] / "src" / "hackingtool" / "catalog"

    rows: list[dict[str, Any]] = []
    if not catalog_dir.is_dir():
        return rows
    for path in sorted(catalog_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        header = data.get("category") or {}
        category = str(header.get("merge_into") or header.get("title") or path.stem)
        for entry in data.get("tools") or []:
            if not isinstance(entry, dict) or not entry.get("title"):
                continue
            rows.append({
                "title": str(entry.get("title")),
                "description": str(entry.get("description") or ""),
                "category": category,
                "category_label": category,
                "icon": str(header.get("icon") or "•"),
                "project_url": str(entry.get("project_url") or entry.get("url") or ""),
                "kind": str(entry.get("kind") or "install"),
                "tags": [str(x) for x in (entry.get("tags") or [])],
                "lab_safe_notes": str(entry.get("lab_safe_notes") or ""),
                "archived": False,
                "archived_reason": "",
            })
    return rows


def load_catalog() -> list[dict[str, Any]]:
    try:
        return _from_engine()
    except Exception:
        return _from_yaml()


def catalog_summary() -> dict[str, Any]:
    tools = load_catalog()
    categories: dict[str, int] = {}
    for tool in tools:
        categories[tool["category"]] = categories.get(tool["category"], 0) + 1
    return {
        "total_tools": len(tools),
        "total_categories": len(categories),
        "categories": categories,
    }
