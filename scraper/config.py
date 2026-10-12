"""配置加载：合并 defaults 与各站点定义。"""
from __future__ import annotations

import os
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "sites.yaml"
)


def load_config(path: str | None = None) -> dict:
    """读取 sites.yaml，并把 defaults 合并进每个站点配置。"""
    path = path or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    defaults = data.get("defaults", {})
    sites = data.get("sites", {})
    for name, cfg in sites.items():
        merged = {**defaults, **cfg}
        sites[name] = merged
    data["sites"] = sites
    return data


def get_site(data: dict, site: str) -> dict:
    if site not in data["sites"]:
        raise KeyError(
            f"未配置的站点: {site}；可选: {list(data['sites'].keys())}"
        )
    return data["sites"][site]
