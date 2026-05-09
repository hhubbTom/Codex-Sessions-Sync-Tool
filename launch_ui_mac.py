from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


TOOL_ROOT = Path(__file__).resolve().parent
BACKEND_PATH = TOOL_ROOT / "sync_backend.py"
UI_BG = "#ffffff"
PANEL_BG = "#f8fafc"
TEXT_COLOR = "#1f2937"
MUTED_COLOR = "#6b7280"
ACCENT_COLOR = "#0a84ff"


def display_text(value: object, max_chars: int = 96) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def run_backend(*args: str, codex_home: str | None = None) -> dict:
    cmd = [sys.executable, str(BACKEND_PATH), "--json"]
    if codex_home:
        cmd.extend(["--codex-home", codex_home])
    cmd.extend(args)
    completed = subprocess.run(cmd, capture_output=True, text=True)
    text = (completed.stdout or completed.stderr).strip()
    if not text:
        raise RuntimeError("后端没有返回任何内容。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"后端 JSON 解析失败: {exc}\n\n原始输出:\n{text}") from exc
    if completed.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or text)
    return payload


class MacApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Codex 历史同步工具 (macOS)")
        self.root.geometry("1040x880")
        self.root.minsize(980, 780)
        self.root.configure(bg=UI_BG)

        self.codex_home_var = tk.StringVar(value=str(Path.home() / ".codex"))
        self.repair_cwd_var = tk.StringVar()
        self.move_cwd_var = tk.StringVar(value=str(Path.home() / "Documents" / "Codex"))
        self.target_provider_var = tk.StringVar()
        self.current_status: dict | None = None
        self.backup_map: dict[str, str] = {}
        self.thread_map: dict[str, dict] = {}
        self.cwd_options: list[str] = []

        self._build_ui()
        self.refresh_status()
        self.refresh_cwds()
        self.refresh_threads()

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Helvetica", 13), background=UI_BG, foreground=TEXT_COLOR)
        style.configure("TFrame", background=UI_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Title.TLabel", background=UI_BG, foreground=ACCENT_COLOR, font=("Helvetica", 22, "bold"))
        style.configure("Muted.TLabel", background=UI_BG, foreground=MUTED_COLOR, font=("Helvetica", 12))
        style.configure("Info.TLabel", background=UI_BG, foreground=TEXT_COLOR, font=("Helvetica", 12))
        style.configure("TLabelframe", background=PANEL_BG, padding=10, borderwidth=1, relief="solid")
        style.configure(
            "TLabelframe.Label",
            background=UI_BG,
            foreground=TEXT_COLOR,
            font=("Helvetica", 13, "bold"),
        )
        style.configure("TEntry", fieldbackground=UI_BG, padding=4)
        style.configure("TCombobox", fieldbackground=UI_BG, padding=4)
        style.configure("TButton", padding=(14, 6), font=("Helvetica", 12, "bold"))
        style.configure(
            "Treeview",
            rowheight=30,
            background=UI_BG,
            fieldbackground=UI_BG,
            foreground=TEXT_COLOR,
            borderwidth=0,
            font=("Helvetica", 12),
        )
        style.configure(
            "Treeview.Heading",
            background="#f3f4f6",
            foreground="#111827",
            font=("Helvetica", 12, "bold"),
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", "#d7ebff")],
            foreground=[("selected", "#111827")],
        )

        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)

        title = ttk.Label(frame, text="Codex History Sync", style="Title.TLabel")
        title.pack(anchor="w")

        warning = ttk.Label(
            frame,
            text="建议先关闭 Codex Desktop 再执行同步或恢复；mac 版会直接调用同一套后端逻辑。",
            style="Muted.TLabel",
        )
        warning.pack(anchor="w", pady=(4, 10))

        path_row = ttk.Frame(frame)
        path_row.pack(fill="x", pady=(0, 8))
        ttk.Label(path_row, text="Codex Home:", style="Info.TLabel").pack(side="left")
        ttk.Entry(path_row, textvariable=self.codex_home_var).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(path_row, text="刷新状态", command=self.refresh_status).pack(side="left")

        repair_path_row = ttk.Frame(frame)
        repair_path_row.pack(fill="x", pady=(0, 8))
        ttk.Label(repair_path_row, text="修复目标 cwd:", style="Info.TLabel").pack(side="left")
        ttk.Entry(repair_path_row, textvariable=self.repair_cwd_var).pack(
            side="left", fill="x", expand=True, padx=(8, 8)
        )
        ttk.Button(repair_path_row, text="选择文件夹", command=self.choose_repair_cwd).pack(side="left")

        self.provider_label = ttk.Label(frame, text="当前 provider:", style="Info.TLabel")
        self.provider_label.pack(anchor="w")
        self.provider_kind_label = ttk.Label(frame, text="provider 类型:", style="Info.TLabel")
        self.provider_kind_label.pack(anchor="w")
        self.provider_source_label = ttk.Label(frame, text="provider 来源:", style="Info.TLabel")
        self.provider_source_label.pack(anchor="w")
        self.model_label = ttk.Label(frame, text="当前模型:", style="Info.TLabel")
        self.model_label.pack(anchor="w")
        self.summary_label = ttk.Label(frame, text="线程总数:", style="Info.TLabel")
        self.summary_label.pack(anchor="w")
        self.db_label = ttk.Label(frame, text="数据库:", style="Info.TLabel")
        self.db_label.pack(anchor="w", pady=(0, 8))

        target_row = ttk.Frame(frame)
        target_row.pack(fill="x", pady=(0, 10))
        ttk.Label(target_row, text="目标 provider:", style="Info.TLabel").pack(side="left")
        ttk.Entry(target_row, textvariable=self.target_provider_var).pack(
            side="left", fill="x", expand=True, padx=(8, 8)
        )
        ttk.Button(target_row, text="使用当前 provider", command=self.use_current_provider).pack(side="left")

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(0, 10))
        ttk.Button(button_row, text="同步到目标 provider", command=self.sync_now).pack(side="left")
        ttk.Button(button_row, text="手动备份", command=self.manual_backup).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="恢复最新备份", command=self.restore_latest).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="打开备份目录", command=self.open_backup_dir).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="修复导入会话", command=self.repair_imported_sessions).pack(side="left", padx=(8, 0))

        panes = ttk.Frame(frame)
        panes.pack(fill="x", pady=(0, 8))
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=0)

        providers_box = ttk.LabelFrame(panes, text="Provider 统计", padding=8)
        providers_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.providers = ttk.Treeview(
            providers_box,
            columns=("provider", "count", "current"),
            show="headings",
            height=3,
        )
        self.providers.heading("provider", text="Provider")
        self.providers.heading("count", text="线程数")
        self.providers.heading("current", text="当前")
        self.providers.column("provider", width=180, anchor="w")
        self.providers.column("count", width=100, anchor="center")
        self.providers.column("current", width=80, anchor="center")
        self.providers.pack(fill="x")
        self.providers.bind("<<TreeviewSelect>>", self.use_selected_provider)

        backups_box = ttk.LabelFrame(panes, text="备份列表", padding=8)
        backups_box.grid(row=0, column=1, sticky="nsew")
        self.backup_list = tk.Listbox(backups_box)
        self.backup_list.configure(height=4)
        self.backup_list.pack(fill="x")
        ttk.Button(backups_box, text="恢复选中备份", command=self.restore_selected).pack(anchor="w", pady=(8, 0))

        move_box = ttk.LabelFrame(frame, text="手动归类会话", padding=8)
        move_box.pack(fill="both", expand=True, pady=(4, 0))
        move_path_row = ttk.Frame(move_box)
        move_path_row.pack(fill="x", pady=(0, 8))
        ttk.Label(move_path_row, text="目标 cwd:", style="Info.TLabel").pack(side="left")
        self.move_cwd_combo = ttk.Combobox(move_path_row, textvariable=self.move_cwd_var, values=(), state="normal")
        self.move_cwd_combo.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(move_path_row, text="公共空间", command=self.use_public_cwd).pack(side="left")
        ttk.Button(move_path_row, text="选择文件夹", command=self.choose_move_cwd).pack(side="left", padx=(8, 0))
        ttk.Button(move_path_row, text="移动选中会话", command=self.move_selected_thread).pack(side="left", padx=(8, 0))
        ttk.Button(move_path_row, text="刷新列表", command=self.refresh_classification_lists).pack(side="left", padx=(8, 0))

        thread_table = ttk.Frame(move_box)
        thread_table.pack(fill="both", expand=True)
        thread_table.rowconfigure(0, weight=1)
        thread_table.columnconfigure(0, weight=1)
        self.threads = ttk.Treeview(
            thread_table,
            columns=("title", "cwd", "updated", "id"),
            show="headings",
            height=16,
        )
        self.threads.heading("title", text="标题")
        self.threads.heading("cwd", text="当前 cwd")
        self.threads.heading("updated", text="更新时间")
        self.threads.heading("id", text="Thread ID")
        self.threads.column("title", width=280, anchor="w", stretch=True)
        self.threads.column("cwd", width=420, anchor="w", stretch=True)
        self.threads.column("updated", width=110, anchor="center", stretch=False)
        self.threads.column("id", width=250, anchor="w", stretch=False)
        self.threads.grid(row=0, column=0, sticky="nsew")
        thread_y = ttk.Scrollbar(thread_table, orient="vertical", command=self.threads.yview)
        thread_y.grid(row=0, column=1, sticky="ns")
        thread_x = ttk.Scrollbar(thread_table, orient="horizontal", command=self.threads.xview)
        thread_x.grid(row=1, column=0, sticky="ew")
        self.threads.configure(yscrollcommand=thread_y.set, xscrollcommand=thread_x.set)

        log_box = ttk.LabelFrame(frame, text="日志", padding=8)
        log_box.pack(fill="x", pady=(8, 0))
        self.log = tk.Text(log_box, height=5, wrap="word")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def get_codex_home(self) -> str:
        return self.codex_home_var.get().strip()

    def get_repair_cwd(self) -> str:
        return self.repair_cwd_var.get().strip()

    def get_move_cwd(self) -> str:
        return self.move_cwd_var.get().strip()

    def refresh_status(self) -> None:
        try:
            payload = run_backend("status", codex_home=self.get_codex_home())
        except Exception as exc:
            messagebox.showerror("刷新失败", str(exc))
            self.append_log(f"刷新失败: {exc}")
            return

        self.current_status = payload
        self.provider_label.config(text=f"当前 provider: {payload['current_provider']}")
        self.provider_kind_label.config(text=f"provider 类型: {payload.get('current_provider_kind') or '未识别'}")
        self.provider_source_label.config(
            text=f"provider 来源: {payload.get('current_provider_source') or '未识别'}"
        )
        self.model_label.config(text=f"当前模型: {payload.get('current_model') or '未读取到'}")
        if not self.target_provider_var.get().strip():
            self.target_provider_var.set(str(payload["current_provider"]))
        self.summary_label.config(
            text=(
                f"线程总数: {payload['total_threads']}    可同步线程: {payload['movable_threads']}"
                f"    可同步会话文件: {payload.get('movable_sessions', 0)}"
                f"    跨设备待修复: {payload.get('repair_candidates', 0)}"
            )
        )
        self.db_label.config(text=f"数据库: {payload['db_path']}")

        for item in self.providers.get_children():
            self.providers.delete(item)
        for row in payload["provider_counts"]:
            current = "是" if row["provider"] == payload["current_provider"] else ""
            self.providers.insert("", "end", values=(row["provider"], row["count"], current))

        self.backup_list.delete(0, "end")
        self.backup_map = {}
        for backup in payload["backups"]:
            label = f"{backup['modified_at']}    {backup['name']}"
            self.backup_map[label] = backup["path"]
            self.backup_list.insert("end", label)

        self.append_log(
            f"状态已刷新。当前 provider={payload['current_provider']}，可同步线程={payload['movable_threads']}，"
            f"provider 来源={payload.get('current_provider_source') or 'unknown'}，"
            f"可同步会话文件={payload.get('movable_sessions', 0)}，"
            f"跨设备待修复={payload.get('repair_candidates', 0)}。"
        )

    def sync_now(self) -> None:
        if not self.current_status:
            self.refresh_status()
        if self.current_status:
            movable_threads = int(self.current_status.get("movable_threads", 0))
            movable_sessions = int(self.current_status.get("movable_sessions", 0))
            if movable_threads <= 0 and movable_sessions <= 0:
                messagebox.showinfo("无需同步", "当前已经没有需要迁移到当前 provider 的线程或会话文件。")
                self.append_log("同步跳过：没有需要迁移的线程或会话文件。")
                return
        target_provider = self.target_provider_var.get().strip()
        if not target_provider:
            messagebox.showerror("目标 provider 为空", "请先输入目标 provider。")
            self.append_log("同步失败：目标 provider 为空。")
            return
        if not messagebox.askokcancel(
            "确认同步",
            f"将其他 provider 的线程统一归到 {target_provider}，且会先自动备份数据库。",
        ):
            self.append_log("用户取消了同步。")
            return
        try:
            payload = run_backend("sync", "--target-provider", target_provider, codex_home=self.get_codex_home())
            self.append_log(
                f"同步完成。目标 provider={payload['target_provider']}，已移动 {payload['updated_rows']} 条线程。"
            )
            session_sync = payload.get("session_sync", {})
            session_stats = session_sync.get("stats", {}) if isinstance(session_sync, dict) else {}
            if session_stats:
                self.append_log(
                    f"会话文件同步：已更新 {session_stats.get('updated_files', 0)} 个，"
                    f"已是当前 provider {session_stats.get('already_current', 0)} 个。"
                )
            if isinstance(session_sync, dict) and session_sync.get("backup_dir"):
                self.append_log(f"会话文件备份目录: {session_sync['backup_dir']}")
            self.append_log(f"数据库备份文件: {payload['backup_path']}")
            self.refresh_status()
            messagebox.showinfo("同步完成", "同步完成。若历史列表没有立刻刷新，重开一次 Codex 即可。")
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc))
            self.append_log(f"同步失败: {exc}")

    def use_current_provider(self) -> None:
        if not self.current_status:
            self.refresh_status()
        if self.current_status:
            self.target_provider_var.set(str(self.current_status["current_provider"]))
            self.append_log(f"目标 provider 已设置为当前值: {self.current_status['current_provider']}")

    def use_selected_provider(self, _event: object) -> None:
        selection = self.providers.selection()
        if not selection:
            return
        values = self.providers.item(selection[0], "values")
        if not values:
            return
        self.target_provider_var.set(str(values[0]))

    def manual_backup(self) -> None:
        try:
            payload = run_backend("backup", codex_home=self.get_codex_home())
            self.append_log(f"手动备份完成: {payload['backup_path']}")
            self.refresh_status()
        except Exception as exc:
            messagebox.showerror("备份失败", str(exc))
            self.append_log(f"备份失败: {exc}")

    def restore_latest(self) -> None:
        if not messagebox.askokcancel("确认恢复", "将恢复最新备份，并在恢复前自动创建安全备份。"):
            self.append_log("用户取消了恢复最新备份。")
            return
        try:
            payload = run_backend("restore", codex_home=self.get_codex_home())
            self.append_log(f"已恢复最新备份: {payload['restored_from']}")
            self.append_log(f"恢复前安全备份: {payload['safety_backup']}")
            self.refresh_status()
            messagebox.showinfo("恢复完成", "恢复完成。建议重开一次 Codex 再看历史列表。")
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            self.append_log(f"恢复失败: {exc}")

    def restore_selected(self) -> None:
        selection = self.backup_list.curselection()
        if not selection:
            messagebox.showwarning("未选择备份", "先在右侧选一个备份。")
            return
        label = self.backup_list.get(selection[0])
        backup_path = self.backup_map.get(label)
        if not backup_path:
            messagebox.showerror("恢复失败", "无法解析选中的备份路径。")
            return
        if not messagebox.askokcancel("确认恢复", f"将恢复这个备份：\n{backup_path}\n\n恢复前会先自动生成一份安全备份。"):
            self.append_log("用户取消了恢复。")
            return
        try:
            payload = run_backend("restore", "--backup", backup_path, codex_home=self.get_codex_home())
            self.append_log(f"恢复完成。来源备份: {payload['restored_from']}")
            self.append_log(f"恢复前安全备份: {payload['safety_backup']}")
            self.refresh_status()
            messagebox.showinfo("恢复完成", "恢复完成。建议重开一次 Codex 再看历史列表。")
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            self.append_log(f"恢复失败: {exc}")

    def open_backup_dir(self) -> None:
        if not self.current_status:
            self.refresh_status()
        backup_dir = self.current_status.get("backup_dir") if self.current_status else None
        if not backup_dir:
            messagebox.showerror("打开失败", "还没有读取到备份目录。")
            return
        path = Path(backup_dir)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(path)], check=False)
        self.append_log(f"已打开备份目录: {path}")

    def choose_repair_cwd(self) -> None:
        from tkinter import filedialog

        initial_dir = self.get_repair_cwd() or str(Path.home())
        selected = filedialog.askdirectory(title="选择修复后归属的项目文件夹", initialdir=initial_dir)
        if selected:
            self.repair_cwd_var.set(selected)
            self.append_log(f"修复目标 cwd 已设置为: {selected}")

    def use_public_cwd(self) -> None:
        public_cwd = str(Path.home() / "Documents" / "Codex")
        self.move_cwd_var.set(public_cwd)
        self.append_log(f"移动目标 cwd 已设置为公共空间: {public_cwd}")

    def choose_move_cwd(self) -> None:
        from tkinter import filedialog

        initial_dir = self.get_move_cwd() or str(Path.home())
        selected = filedialog.askdirectory(title="选择会话移动后的项目文件夹", initialdir=initial_dir)
        if selected:
            self.move_cwd_var.set(selected)
            self.append_log(f"移动目标 cwd 已设置为: {selected}")

    def refresh_cwds(self) -> None:
        try:
            payload = run_backend("list-cwds", codex_home=self.get_codex_home())
        except Exception as exc:
            messagebox.showerror("刷新工作区失败", str(exc))
            self.append_log(f"刷新工作区失败: {exc}")
            return

        public_cwd = str(Path.home() / "Documents" / "Codex")
        options = [row["cwd"] for row in payload.get("cwds", [])]
        if public_cwd not in options:
            options.insert(0, public_cwd)
        self.cwd_options = options
        self.move_cwd_combo.configure(values=options)
        if not self.get_move_cwd() and options:
            self.move_cwd_var.set(options[0])
        self.append_log(f"工作区列表已刷新，共 {len(options)} 个。")

    def refresh_classification_lists(self) -> None:
        self.refresh_cwds()
        self.refresh_threads()

    def refresh_threads(self) -> None:
        try:
            payload = run_backend("list-threads", "--limit", "300", codex_home=self.get_codex_home())
        except Exception as exc:
            messagebox.showerror("刷新会话失败", str(exc))
            self.append_log(f"刷新会话失败: {exc}")
            return

        for item in self.threads.get_children():
            self.threads.delete(item)
        self.thread_map = {}
        for row in payload.get("threads", []):
            thread_id = row["id"]
            self.thread_map[thread_id] = row
            self.threads.insert(
                "",
                "end",
                iid=thread_id,
                values=(
                    display_text(row.get("display_title") or row.get("title", ""), 72),
                    display_text(row.get("cwd", ""), 92),
                    row.get("updated_at", ""),
                    thread_id,
                ),
            )
        self.append_log(f"会话列表已刷新，共 {len(self.thread_map)} 条。")

    def move_selected_thread(self) -> None:
        selection = self.threads.selection()
        if not selection:
            messagebox.showwarning("未选择会话", "先在手动归类列表里选一个会话。")
            return

        thread_id = selection[0]
        row = self.thread_map.get(thread_id, {})
        target_cwd = self.get_move_cwd()
        if not target_cwd:
            messagebox.showwarning("缺少目标 cwd", "请先填写或选择目标 cwd。")
            return

        title = row.get("title", thread_id)
        current_cwd = row.get("cwd", "")
        message = (
            f"将移动会话：\n{title}\n\n"
            f"当前 cwd:\n{current_cwd}\n\n"
            f"目标 cwd:\n{target_cwd}\n\n"
            "会先自动备份数据库和 jsonl。"
        )
        if not messagebox.askokcancel("确认移动会话", message):
            self.append_log("用户取消了移动会话。")
            return

        try:
            payload = run_backend(
                "move-thread",
                "--thread-id",
                thread_id,
                "--cwd",
                target_cwd,
                codex_home=self.get_codex_home(),
            )
            moved_count = len(payload.get("moved_threads") or [])
            skipped_count = len(payload.get("skipped_threads") or [])
            self.append_log(f"会话移动完成。已移动 {moved_count} 条，跳过 {skipped_count} 条。")
            self.append_log(f"目标 cwd: {payload.get('target_cwd')}")
            self.append_log(f"数据库备份: {payload['db_backup']}")
            if payload.get("session_backup_dir"):
                self.append_log(f"会话文件备份目录: {payload['session_backup_dir']}")
            self.refresh_status()
            self.refresh_threads()
            messagebox.showinfo("移动完成", "移动完成。请重开一次 Codex Desktop 再看左侧历史列表。")
        except Exception as exc:
            messagebox.showerror("移动失败", str(exc))
            self.append_log(f"移动失败: {exc}")

    def repair_imported_sessions(self) -> None:
        if not self.current_status:
            self.refresh_status()
        repair_candidates = int((self.current_status or {}).get("repair_candidates", 0))
        if repair_candidates <= 0:
            messagebox.showinfo("无需修复", "当前没有检测到需要跨设备修复的导入会话。")
            self.append_log("修复跳过：没有检测到跨设备导入会话。")
            return

        message = (
            "将会修复从其他设备复制过来的会话记录。\n\n"
            "这会同时更新 jsonl 会话头部和本地线程数据库，并先自动备份。\n"
            "如果填写了修复目标 cwd，会话会归到该项目文件夹；未填写时才使用当前设备最近在用的本地路径。"
        )
        if not messagebox.askokcancel("确认修复导入会话", message):
            self.append_log("用户取消了修复导入会话。")
            return

        try:
            backend_args = ["repair"]
            repair_cwd = self.get_repair_cwd()
            if repair_cwd:
                backend_args.extend(["--cwd", repair_cwd])
            payload = run_backend(*backend_args, codex_home=self.get_codex_home())
            repaired_count = len(payload.get("repaired_threads") or [])
            skipped_count = len(payload.get("skipped_threads") or [])
            self.append_log(f"导入会话修复完成。已修复 {repaired_count} 条，跳过 {skipped_count} 条。")
            self.append_log(f"目标 cwd: {payload.get('target_cwd')}")
            self.append_log(f"数据库备份: {payload['db_backup']}")
            if payload.get("session_backup_dir"):
                self.append_log(f"会话文件备份目录: {payload['session_backup_dir']}")
            self.refresh_status()
            messagebox.showinfo("修复完成", "修复完成。请重开一次 Codex Desktop 再看左侧历史列表。")
        except Exception as exc:
            messagebox.showerror("修复失败", str(exc))
            self.append_log(f"修复失败: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="macOS GUI for Codex history sync tool")
    parser.add_argument("--smoke-test", action="store_true", help="Run a backend connectivity check and exit")
    parser.add_argument("--codex-home", help="Override Codex home directory for smoke testing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.smoke_test:
        payload = run_backend("status", codex_home=args.codex_home)
        print(
            f"Smoke test OK: provider={payload['current_provider']} "
            f"movable_threads={payload['movable_threads']} "
            f"movable_sessions={payload.get('movable_sessions', 0)} "
            f"repair_candidates={payload.get('repair_candidates', 0)}"
        )
        return 0

    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("aqua")
    except tk.TclError:
        pass
    MacApp(root)

    # Try hard to bring the window to the foreground on macOS launch.
    root.update_idletasks()
    root.deiconify()
    root.lift()
    try:
        root.focus_force()
    except tk.TclError:
        pass
    try:
        root.attributes("-topmost", True)
        root.after(250, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to set frontmost of the first process whose unix id is '
                f'{os.getpid()} to true',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
