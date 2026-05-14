#!/usr/bin/env python3

import copy
import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any


APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / "config"
IGNORED_CONFIG_FILES = {"settings.json"}
OBSOLETE_CONFIG_KEYS = {"retag_existing"}
DEFAULT_APP_ID = "950096963"
DEFAULT_APP_SECRET = "979549437fcc4a3faad4867b5cd25dcb"

DEFAULT_CONFIG = {
    "country": "",
    "active": 0,
    "download_path": "./downloads/",
    "download_quality": "hifi",
    "album_folder_format": "{artist} - {album}{explicit} ({year})  [{quality}]",
    "track_filename_format": "{track_number}. {title}",
    "quality_format": "{bit_depth}B-{sample_rate}kHz",
    "artist_tag_separator": ", ",
    "embed_cover": True,
    "save_cover": True,
    "save_description": True,
    "skip_existing": True,
    "verify_tls": True,
    "request_timeout": 45,
    "download_threads": 3,
    "qobuz": {
        "app_id": DEFAULT_APP_ID,
        "app_secret": DEFAULT_APP_SECRET,
        "user_id": "",
        "auth_token": "",
    },
}

QOBUZ_FIELDS = ("app_id", "app_secret", "user_id", "auth_token")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)
        file.write("\n")


def active_to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value == 1 else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 0


def country_to_filename(country: str) -> str:
    value = country.strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_-]", "", value)
    if not value:
        raise ValueError("Country name is required")
    return value + ".json"


def normalize_config(data: dict[str, Any], fallback_country: str = "") -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)

    for key, value in data.items():
        if key not in {"qobuz", "app_id", "app_secret", "user_id", "auth_token"} | OBSOLETE_CONFIG_KEYS:
            config[key] = value

    qobuz = data.get("qobuz") if isinstance(data.get("qobuz"), dict) else data
    config["country"] = str(data.get("country") or fallback_country)
    config["active"] = active_to_int(data.get("active", 0))
    config["qobuz"] = {
        field: str(qobuz.get(field) or config["qobuz"].get(field, ""))
        for field in QOBUZ_FIELDS
    }
    return config


class ConfigGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("qbdl config generator")
        self.root.geometry("760x430")
        self.current_path: Path | None = None
        self.current_data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)

        self.country_var = tk.StringVar()
        self.active_var = tk.IntVar(value=0)
        self.download_threads_var = tk.StringVar(value=str(DEFAULT_CONFIG["download_threads"]))
        self.field_vars = {field: tk.StringVar() for field in QOBUZ_FIELDS}
        self.entry_widgets: list[ttk.Entry] = []

        self.build_ui()
        self.reload_configs()
        self.new_config()

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        left.rowconfigure(0, weight=1)

        self.config_list = tk.Listbox(left, width=26, height=18, exportselection=False)
        self.config_list.grid(row=0, column=0, sticky="ns")
        self.config_list.bind("<<ListboxSelect>>", self.on_select)

        left_buttons = ttk.Frame(left)
        left_buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(left_buttons, text="New", command=self.new_config).pack(side=tk.LEFT)
        ttk.Button(left_buttons, text="Reload", command=self.reload_configs).pack(side=tk.LEFT, padx=(6, 0))

        form = ttk.Frame(outer)
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Country filename").grid(row=0, column=0, sticky="w", pady=6)
        country_entry = ttk.Entry(form, textvariable=self.country_var)
        country_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.entry_widgets.append(country_entry)
        ttk.Label(form, text=".json").grid(row=0, column=2, sticky="w", padx=(6, 0), pady=6)

        ttk.Label(form, text="Active").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Checkbutton(form, text="Use this account for downloads", variable=self.active_var).grid(
            row=1, column=1, sticky="w", pady=6
        )

        ttk.Label(form, text="download_threads").grid(row=2, column=0, sticky="w", pady=6)
        threads_entry = ttk.Entry(form, textvariable=self.download_threads_var, width=8)
        threads_entry.grid(row=2, column=1, sticky="w", pady=6)
        self.entry_widgets.append(threads_entry)

        for row, field in enumerate(QOBUZ_FIELDS, start=3):
            ttk.Label(form, text=field).grid(row=row, column=0, sticky="w", pady=6)
            entry = ttk.Entry(form, textvariable=self.field_vars[field])
            entry.grid(
                row=row, column=1, columnspan=2, sticky="ew", pady=6
            )
            self.entry_widgets.append(entry)

        actions = ttk.Frame(form)
        actions.grid(row=7, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(actions, text="Delete", command=self.delete_config).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(actions, text="Save", command=self.save_config).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.install_entry_shortcuts()

    def install_entry_shortcuts(self) -> None:
        menu = tk.Menu(self.root, tearoff=False)

        def selected_entry() -> ttk.Entry | None:
            widget = self.root.focus_get()
            return widget if widget in self.entry_widgets else None

        menu.add_command(label="Cut", command=lambda: self.cut_entry(selected_entry()))
        menu.add_command(label="Copy", command=lambda: self.copy_entry(selected_entry()))
        menu.add_command(label="Paste", command=lambda: self.paste_entry(selected_entry()))
        menu.add_separator()
        menu.add_command(label="Select all", command=lambda: self.select_all_entry(selected_entry()))

        for entry in self.entry_widgets:
            entry.bind("<Control-KeyPress>", self.entry_shortcut)
            entry.bind("<Control-Insert>", lambda event: self.copy_entry(event.widget))
            entry.bind("<Shift-Insert>", lambda event: self.paste_entry(event.widget))
            entry.bind("<Button-3>", lambda event: self.show_entry_menu(event, menu))

    def entry_shortcut(self, event) -> str | None:
        key = str(event.keysym).lower()
        keycode = event.keycode

        if key in {"a", "ф"} or keycode == 65:
            return self.select_all_entry(event.widget)
        if key in {"c", "с"} or keycode == 67:
            return self.copy_entry(event.widget)
        if key in {"v", "м"} or keycode == 86:
            return self.paste_entry(event.widget)
        if key in {"x", "ч"} or keycode == 88:
            return self.cut_entry(event.widget)
        return None

    def selected_range(self, entry: ttk.Entry) -> tuple[int, int] | None:
        try:
            return int(entry.index("sel.first")), int(entry.index("sel.last"))
        except tk.TclError:
            return None

    def copy_entry(self, entry: ttk.Entry | None) -> str:
        if entry is None:
            return "break"
        selected = self.selected_range(entry)
        if selected:
            start, end = selected
            self.root.clipboard_clear()
            self.root.clipboard_append(entry.get()[start:end])
        return "break"

    def cut_entry(self, entry: ttk.Entry | None) -> str:
        if entry is None:
            return "break"
        selected = self.selected_range(entry)
        if selected:
            start, end = selected
            self.root.clipboard_clear()
            self.root.clipboard_append(entry.get()[start:end])
            entry.delete(start, end)
        return "break"

    def paste_entry(self, entry: ttk.Entry | None) -> str:
        if entry is None:
            return "break"
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        selected = self.selected_range(entry)
        if selected:
            entry.delete(*selected)
        entry.insert("insert", text)
        return "break"

    def select_all_entry(self, entry: ttk.Entry | None) -> str:
        if entry is None:
            return "break"
        entry.selection_range(0, tk.END)
        entry.icursor(tk.END)
        return "break"

    def show_entry_menu(self, event, menu: tk.Menu) -> str:
        event.widget.focus_set()
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def config_files(self) -> list[Path]:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(
            path
            for path in CONFIG_DIR.glob("*.json")
            if path.name.lower() not in IGNORED_CONFIG_FILES
        )

    def reload_configs(self) -> None:
        self.config_list.delete(0, tk.END)
        for path in self.config_files():
            self.config_list.insert(tk.END, path.name)
        self.status_var.set(f"Config folder: {CONFIG_DIR}")

    def new_config(self) -> None:
        self.current_path = None
        self.current_data = copy.deepcopy(DEFAULT_CONFIG)
        self.country_var.set("")
        self.active_var.set(0)
        self.download_threads_var.set(str(self.current_data.get("download_threads", 3)))
        for field in QOBUZ_FIELDS:
            self.field_vars[field].set(self.current_data["qobuz"].get(field, ""))
        self.config_list.selection_clear(0, tk.END)
        self.status_var.set("New config")

    def on_select(self, _event=None) -> None:
        selection = self.config_list.curselection()
        if not selection:
            return
        filename = self.config_list.get(selection[0])
        path = CONFIG_DIR / filename
        try:
            raw = load_json(path)
            config = normalize_config(raw, fallback_country=path.stem)
        except Exception as error:
            messagebox.showerror("Load failed", str(error))
            return

        self.current_path = path
        self.current_data = config
        self.country_var.set(config.get("country") or path.stem)
        self.active_var.set(active_to_int(config.get("active", 0)))
        self.download_threads_var.set(str(config.get("download_threads", 3)))
        for field in QOBUZ_FIELDS:
            self.field_vars[field].set(config["qobuz"].get(field, ""))
        self.status_var.set(f"Loaded {path.name}")

    def save_config(self) -> None:
        try:
            filename = country_to_filename(self.country_var.get())
        except ValueError as error:
            messagebox.showerror("Save failed", str(error))
            return

        target = CONFIG_DIR / filename
        if self.current_path and target != self.current_path and target.exists():
            replace = messagebox.askyesno("Replace config", f"{target.name} already exists. Replace it?")
            if not replace:
                return

        try:
            download_threads = max(1, int(self.download_threads_var.get().strip()))
        except ValueError:
            messagebox.showerror("Save failed", "download_threads must be a positive whole number")
            return

        config = normalize_config(self.current_data, fallback_country=target.stem)
        config["country"] = target.stem
        config["active"] = active_to_int(self.active_var.get())
        config["download_threads"] = download_threads
        config["qobuz"] = {
            field: self.field_vars[field].get().strip()
            for field in QOBUZ_FIELDS
        }

        try:
            write_json(target, config)
            if self.current_path and target != self.current_path and self.current_path.exists():
                self.current_path.unlink()
        except Exception as error:
            messagebox.showerror("Save failed", str(error))
            return

        self.current_path = target
        self.current_data = config
        self.reload_configs()
        names = list(self.config_list.get(0, tk.END))
        if target.name in names:
            index = names.index(target.name)
            self.config_list.selection_set(index)
            self.config_list.see(index)
        self.status_var.set(f"Saved {target.name}")

    def delete_config(self) -> None:
        if not self.current_path:
            return
        delete = messagebox.askyesno("Delete config", f"Delete {self.current_path.name}?")
        if not delete:
            return
        try:
            self.current_path.unlink(missing_ok=True)
        except Exception as error:
            messagebox.showerror("Delete failed", str(error))
            return
        self.reload_configs()
        self.new_config()


def main() -> None:
    root = tk.Tk()
    ConfigGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
