"""Persisted Daily Trend and ML item selection for Auto Report aliases."""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import pandas as pd
import yaml


SERVICES = ("daily_trend", "mlmode")
_ROOT_KEYS = {"version", *SERVICES}
_OPTION_KEYS = {"time_column", "split_columns"}


def eligible_aliases(formatter):
    """Return canonical Auto Report measurement aliases in formatter order."""
    if "ALIAS" not in formatter or "REPORT ORDER" not in formatter:
        return []
    order = pd.to_numeric(formatter["REPORT ORDER"], errors="coerce")
    mask = order.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value))))
    return list(dict.fromkeys(formatter.loc[mask, "ALIAS"].dropna().astype(str).str.strip().loc[lambda s: s.ne("")].tolist()))


def _validate_options(options, context):
    if not isinstance(options, dict):
        raise ValueError(f"{context} must be a mapping")
    extra = set(options) - _OPTION_KEYS
    if extra:
        raise ValueError(f"{context} has unsupported keys: {', '.join(sorted(map(str, extra)))}")
    time_column = options.get("time_column", "")
    split_columns = options.get("split_columns", [])
    if not isinstance(time_column, str):
        raise ValueError(f"{context}.time_column must be a string")
    if not isinstance(split_columns, list) or any(not isinstance(value, str) for value in split_columns):
        raise ValueError(f"{context}.split_columns must be a list of strings")
    return {"time_column": time_column, "split_columns": list(split_columns)}


def _validate_catalog(catalog):
    if not isinstance(catalog, dict):
        raise ValueError("report_items catalog must be a mapping")
    extra = set(catalog) - _ROOT_KEYS
    if extra:
        raise ValueError(f"report_items has unsupported keys: {', '.join(sorted(map(str, extra)))}")
    if "version" in catalog and (type(catalog["version"]) is not int or catalog["version"] != 1):
        raise ValueError("report_items.version must be 1")
    for service in SERVICES:
        products = catalog.get(service, {})
        if not isinstance(products, dict):
            raise ValueError(f"report_items.{service} must be a mapping")
        for vehicle, items in products.items():
            if not isinstance(vehicle, str) or not vehicle.strip():
                raise ValueError(f"report_items.{service} product keys must be nonempty strings")
            if not isinstance(items, dict):
                raise ValueError(f"report_items.{service}.{vehicle} must map aliases to options")
            for alias, options in items.items():
                if not isinstance(alias, str) or not alias.strip():
                    raise ValueError(f"report_items.{service}.{vehicle} aliases must be nonempty strings")
                _validate_options(options, f"report_items.{service}.{vehicle}.{alias}")
    return catalog


def load_catalog(path):
    """Load and strictly validate the catalog; a missing file means legacy all."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    raw = raw or {}
    if not isinstance(raw, dict) or "version" not in raw:
        raise ValueError("report_items.version is required and must be 1")
    return _validate_catalog(raw)


def select_formatter(formatter, vehicle, service, catalog):
    """Select eligible ALIAS rows and return alias→ML_TABLE column options.

    An absent service/product retains legacy all-eligible behavior. An explicitly
    present empty product map selects no items.
    """
    if service not in SERVICES:
        raise ValueError(f"Unsupported report item service: {service}")
    catalog = _validate_catalog(catalog)
    eligible = eligible_aliases(formatter)
    configured = catalog.get(service, {})
    if vehicle not in configured:
        chosen = set(eligible)
        raw_options = {}
    else:
        raw_options = configured[vehicle]
        unknown = set(raw_options) - set(eligible)
        if unknown:
            raise ValueError(f"report_items.{service}.{vehicle} contains unknown or ineligible aliases: {', '.join(sorted(unknown))}")
        chosen = set(raw_options)
    order = pd.to_numeric(formatter["REPORT ORDER"], errors="coerce")
    finite_order = order.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value))))
    # Filter invalid rows before alias selection so a duplicate alias cannot
    # sneak in via a formatter row that Auto Report itself would not include.
    selected = formatter.loc[finite_order & formatter["ALIAS"].astype("string").isin(chosen)].copy()
    if "CAT2" not in selected:
        selected["CAT2"] = ""
    selected["CAT2"] = selected["CAT2"].fillna("").astype(str).str.strip().replace("", "Uncategorized")
    options = {alias: _validate_options(raw_options.get(alias, {}), f"{service}.{vehicle}.{alias}")
               for alias in eligible if alias in chosen}
    return selected, options


def update_items(path, formatter, vehicle, services, add=None, remove=None, options=None):
    """Apply an explicit add/remove edit and atomically preserve other catalog data.

    On first edit, each edited service/product is materialized from all eligible
    aliases so removing one alias does not accidentally turn the default-all set
    into an empty set.
    """
    path = Path(path)
    services = [services] if isinstance(services, str) else list(services)
    if not services or any(service not in SERVICES for service in services):
        raise ValueError("services must contain daily_trend and/or mlmode")
    eligible = eligible_aliases(formatter)
    allowed = set(eligible)
    additions = [add] if isinstance(add, str) else list(add or [])
    removals = [remove] if isinstance(remove, str) else list(remove or [])
    option_updates = options or {}
    if not isinstance(option_updates, dict):
        raise ValueError("options must map aliases to options")
    touched = set(additions) | set(removals) | set(option_updates)
    invalid = touched - allowed
    if invalid:
        raise ValueError("unknown or ineligible Auto Report aliases: " + ", ".join(sorted(invalid)))
    for alias, value in option_updates.items():
        _validate_options(value, f"options.{alias}")
    catalog = load_catalog(path)
    catalog.setdefault("version", 1)
    for service in SERVICES:
        catalog.setdefault(service, {})
    for service in services:
        products = catalog[service]
        if vehicle not in products:
            products[vehicle] = {alias: {"time_column": "", "split_columns": []} for alias in eligible}
        items = products[vehicle]
        for alias in additions:
            items.setdefault(alias, {"time_column": "", "split_columns": []})
        for alias in removals:
            items.pop(alias, None)
        for alias, value in option_updates.items():
            if alias in items:
                items[alias] = _validate_options(value, f"options.{alias}")
    _validate_catalog(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(catalog, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return catalog
