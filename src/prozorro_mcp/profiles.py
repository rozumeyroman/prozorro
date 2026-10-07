"""Filter profiles: built-in ones in config/filters/, user ones in <output_dir>/Фільтри/.

The active profile is remembered in the database and used by every tool until it is changed.
A user profile is a full copy of a base profile with changes applied, so it can be edited by hand as well.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from .db import Database
from .filter import TenderFilter

ACTIVE_KEY = "active_filter"
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CPV_RX = re.compile(r"^\d{8}-\d$")


class FilterError(ValueError):
    pass


class FilterRegistry:
    def __init__(self, builtin_dir: Path, user_dir: Path, default: str, explicit_file: Path | None = None):
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir
        self.default = explicit_file.stem if explicit_file else default
        self.explicit_file = explicit_file
        self._cache: dict[Path, tuple[float, TenderFilter]] = {}

    # discovery -----------------------------------------------------------------------------------

    def paths(self) -> dict[str, tuple[Path, str]]:
        """name -> (path, "builtin" | "user")."""
        found: dict[str, tuple[Path, str]] = {}
        for source, folder in (("builtin", self.builtin_dir), ("user", self.user_dir)):
            if folder.is_dir():
                for p in sorted(folder.glob("*.yaml")):
                    found.setdefault(p.stem, (p, source))
        if self.explicit_file:
            found[self.explicit_file.stem] = (self.explicit_file, "file")
        return found

    def load(self, name: str) -> TenderFilter:
        paths = self.paths()
        if name not in paths:
            raise FilterError(f"Фільтр «{name}» не знайдено. Доступні: {', '.join(sorted(paths))}")
        path = paths[name][0]
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            f = TenderFilter.from_file(path, name)
        except (yaml.YAMLError, KeyError, re.error) as e:
            raise FilterError(f"Фільтр «{name}» ({path}) містить помилку: {e}") from e
        self._cache[path] = (mtime, f)
        return f

    def active_name(self, db: Database) -> str:
        name = db.get_meta(ACTIVE_KEY) or self.default
        return name if name in self.paths() else self.default

    def active(self, db: Database) -> TenderFilter:
        return self.load(self.active_name(db))

    def resolve(self, db: Database, name: str | None) -> TenderFilter:
        """The named profile for a single call, or the active one."""
        return self.load(name) if name else self.active(db)

    def set_active(self, db: Database, name: str) -> TenderFilter:
        f = self.load(name)
        db.set_meta(ACTIVE_KEY, name)
        return f

    def list(self, db: Database) -> list[dict[str, Any]]:
        active = self.active_name(db)
        out = []
        for name, (path, source) in self.paths().items():
            try:
                summary = self.load(name).summary()
            except FilterError as e:
                summary = {"error": str(e)}
            out.append({"name": name, "active": name == active, "source": source, "path": str(path), **summary})
        return out

    # editing -------------------------------------------------------------------------------------

    def save(self, name: str, base: str | None, changes: dict[str, Any]) -> tuple[TenderFilter, Path]:
        if not NAME_RX.match(name):
            raise FilterError("Назва фільтра: латиниця в нижньому регістрі, цифри, '-' або '_', до 40 символів")
        existing = self.paths().get(name)
        if existing and existing[1] != "user":
            raise FilterError(f"«{name}» — вбудований фільтр; збережіть зміни під іншою назвою")
        source = base or (name if existing else None)
        if not source:
            raise FilterError("Вкажіть base: фільтр, на основі якого створити новий")
        config = copy.deepcopy(self.load(source).config)
        apply_changes(config, changes)
        TenderFilter(config, name)  # validate before writing
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{name}.yaml"
        header = f"# Фільтр «{name}», створено на основі «{source}». Можна редагувати вручну.\n"
        path.write_text(header + yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return self.load(name), path

    def delete(self, db: Database, name: str) -> None:
        existing = self.paths().get(name)
        if not existing or existing[1] != "user":
            raise FilterError(f"Видаляти можна лише власні фільтри; «{name}» таким не є")
        existing[0].unlink()
        if db.get_meta(ACTIVE_KEY) == name:
            db.set_meta(ACTIVE_KEY, self.default)


def _check_cpv(codes: list[str]) -> list[str]:
    bad = [c for c in codes if not CPV_RX.match(c)]
    if bad:
        raise FilterError(f"Неправильний формат коду CPV (потрібно 12345678-9): {', '.join(bad)}")
    return codes


def _add(lst: list[Any], values: list[Any]) -> None:
    for v in values:
        if v not in lst:
            lst.append(v)


def _remove(lst: list[Any], values: list[Any]) -> None:
    for v in values:
        while v in lst:
            lst.remove(v)


def apply_changes(config: dict[str, Any], ch: dict[str, Any]) -> None:
    """Apply a change set (as accepted by the save_filter tool) to a filter config in place."""
    if ch.get("description"):
        config["description"] = ch["description"]
    if ch.get("min_value") is not None:
        config["min_value"]["amount"] = float(ch["min_value"])
    if ch.get("value_scope"):
        config["min_value"]["scope"] = ch["value_scope"]

    strong = config.setdefault("cpv_strong", {})
    group = ch.get("cpv_group") or "custom"
    if ch.get("add_cpv"):
        _add(strong.setdefault(group, []), _check_cpv(ch["add_cpv"]))
    if ch.get("add_cpv_weak"):
        _add(config.setdefault("cpv_weak", []), _check_cpv(ch["add_cpv_weak"]))
    if ch.get("exclude_cpv"):
        _add(config.setdefault("cpv_exclude", []), _check_cpv(ch["exclude_cpv"]))
    if ch.get("remove_cpv"):
        codes = _check_cpv(ch["remove_cpv"])
        for g in list(strong):
            _remove(strong[g], codes)
            if not strong[g]:
                del strong[g]
        _remove(config.get("cpv_weak", []), codes)
        _remove(config.get("cpv_exclude", []), codes)

    kw = config.setdefault("keywords", {})
    include = kw.get("include", [])
    if isinstance(include, list):
        include = kw["include"] = {"keyword": include}
    kw_group = ch.get("keyword_group") or "custom"
    for p in ch.get("add_keywords") or []:
        re.compile(p, re.I)
    if ch.get("add_keywords"):
        _add(include.setdefault(kw_group, []), ch["add_keywords"])
    if ch.get("remove_keywords"):
        for g in list(include):
            _remove(include[g], ch["remove_keywords"])
            if not include[g]:
                del include[g]
    for p in ch.get("add_exclude_keywords") or []:
        re.compile(p, re.I)
    if ch.get("add_exclude_keywords"):
        _add(kw.setdefault("exclude", []), ch["add_exclude_keywords"])
    if ch.get("remove_exclude_keywords"):
        _remove(kw.get("exclude", []), ch["remove_exclude_keywords"])

    pmt = config.setdefault("feed_prefilter", {}).setdefault("procurement_method_types", {})
    if ch.get("add_procurement_method_types"):
        _add(pmt.setdefault("include", []), ch["add_procurement_method_types"])
    if ch.get("remove_procurement_method_types"):
        _remove(pmt.get("include", []), ch["remove_procurement_method_types"])


def ensure_matches(db: Database, f: TenderFilter, force: bool = False) -> int:
    """Re-evaluate all stored tenders under `f` if its rules changed since the last evaluation (no network).

    Returns the number of tenders relevant under `f`.
    """
    meta_key = f"matches:{f.name}"
    # Re-run when the rules changed or tenders were added/updated (e.g. by a sync under another profile).
    stamp = f"{f.key}|{db.tenders_stamp()}"
    if force or db.get_meta(meta_key) != stamp:
        for data in db.tenders_data(db.all_tender_ids()):
            d = f.evaluate(data)
            if d.relevant:
                db.save_match(data["id"], f.name, f.key, d)
            else:
                db.delete_match(data["id"], f.name)
        db.set_meta(meta_key, stamp)
    return db.counts(f.name)["relevant_tenders"]
