import json
import os
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import List, Callable, Optional, Dict, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pynput import mouse, keyboard
from pynput.mouse import Button as MouseButton
from pynput.keyboard import Key, KeyCode, Listener as KeyListener, GlobalHotKeys

# ==============================
# Global Config / Hotkey Manager
# ==============================

APP_TITLE = "AutoClick v1.0.0"
DEFAULT_GEOMETRY = "460x400"
APP_ICON = "click.ico"
CONFIG_FILE = "app_config.json"

def app_dir() -> str:
    if getattr(sys, 'frozen', False):  # PyInstaller 打包环境
        return os.path.dirname(os.path.abspath(sys.executable))
    else:  # 源码运行
        return os.path.dirname(os.path.abspath(__file__))


def default_config() -> dict:
    return {
        "global": {
            "geometry": DEFAULT_GEOMETRY,
            "last_tab": 0
        },
        "recorder": {
            "hotkeys": {"toggle_record": "<f9>", "toggle_play": "<f10>"},
            "params": {"interval": 0.1, "speed": 1.0, "loops": 1, "gap": 0.0, "delay": 0.0},
            "last_file": None
        },
        "clicker": {
            "hotkeys": {"start_stop": "<f7>", "add_marker": "<f6>"},
            "params": {"loops": 1, "delay": 0.0},
            "markers": []  # [{id,x,y,button,interval}]
        },
    }


class ConfigManager:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(app_dir(), CONFIG_FILE)
        self.data = default_config()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                print("配置读取失败，将使用默认配置：", e)
                self.data = default_config()
        else:
            self.data = default_config()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("配置保存失败：", e)


class GlobalHotkeyManager:
    def __init__(self, on_error: Optional[Callable[[str], None]] = None):
        self.listener: Optional[GlobalHotKeys] = None
        self.mapping: Dict[str, Callable[[], None]] = {}
        self.on_error = on_error

    def stop(self):
        try:
            if self.listener:
                self.listener.stop()
        except Exception:
            pass
        self.listener = None

    def set_mapping(self, mapping: Dict[str, Callable[[], None]]):
        self.stop()
        self.mapping = mapping or {}
        if not self.mapping:
            return
        try:
            self.listener = GlobalHotKeys(self.mapping)
            self.listener.start()
        except Exception as e:
            if self.on_error:
                self.on_error(f"无法注册全局热键：{e}")


# ==================
# Clicker Page (AC)
# ==================

@dataclass
class Marker:
    id: int
    x: int
    y: int
    button: str = "left"  # "left" or "right"
    interval: float = 0.2  # seconds after clicking this marker


@dataclass
class ClickConfig:
    loops: int = 1
    delay: float = 0.0  # start delay (seconds)


class MarkerWindow(tk.Toplevel):
    SIZE = 40

    def __init__(self, master, marker_id: int,
                 on_move: Callable[[int, int, int], None],
                 on_close: Callable[[int], None]):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", "#00ff00")
        except Exception:
            pass
        try:
            self.attributes("-alpha", 0.65)
        except Exception:
            pass

        self.configure(bg="#00ff00")
        self.marker_id = marker_id
        self.on_move_cb = on_move
        self.on_close_cb = on_close

        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE,
                                highlightthickness=0, bg="#00ff00", cursor="fleur")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        c = self.SIZE // 2
        r = self.SIZE // 2 - 2
        self.canvas.create_oval(c - r, c - r, c + r, c + r,
                                outline="#00cc66", width=2,
                                fill="#e6ffe6", stipple="gray50")
        self.canvas.create_line(c - 6, c, c + 6, c, fill="red", width=2)
        self.canvas.create_line(c, c - 6, c, c + 6, fill="red", width=2)
        self.text_id = self.canvas.create_text(c + 10, c + 10, text=str(marker_id),
                                               fill="black", font=("Arial", 10, "bold"))

        for seq in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.canvas.bind(seq, getattr(self, f"_{seq.strip('<>').replace('-', '_')}"))
        self.canvas.bind("<Button-3>", self._close_me)

        self._drag_offset = (0, 0)

    def _Button_1(self, event):
        self._start_move(event)

    def _B1_Motion(self, event):
        self._on_move(event)

    def _ButtonRelease_1(self, event):
        self._end_move(event)

    def _center_x(self):
        return self.winfo_x() + self.SIZE // 2

    def _center_y(self):
        return self.winfo_y() + self.SIZE // 2

    def update_number(self, new_id: int):
        self.marker_id = new_id
        self.canvas.itemconfigure(self.text_id, text=str(new_id))

    def _start_move(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _on_move(self, event):
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{int(x)}+{int(y)}")

    def _end_move(self, event):
        self.on_move_cb(self.marker_id, self._center_x(), self._center_y())

    def _close_me(self, _=None):
        mid = self.marker_id
        self.destroy()
        self.on_close_cb(mid)


class ClickRunner:
    def __init__(self, logger: Optional[Callable[[str], None]] = None,
                 on_end: Optional[Callable[[], None]] = None):
        self._mouse_ctrl = mouse.Controller()
        self._stop_event = threading.Event()
        self._is_running = False
        self.logger = logger
        self.on_end = on_end

    def log(self, msg: str):
        if self.logger:
            self.logger(msg)

    def is_running(self) -> bool:
        return self._is_running

    def stop(self):
        self._stop_event.set()

    def start(self, markers: List[Marker], cfg: ClickConfig):
        if not markers:
            self.log("没有标记坐标")
            return False
        if self._is_running:
            return False

        self._stop_event.clear()
        self._is_running = True

        def worker():
            try:
                if cfg.delay > 0:
                    self.log(f"启动延迟 {cfg.delay:.2f}s 后开始")
                    time.sleep(max(0.0, cfg.delay))
                loop_idx = 0
                total_loops = cfg.loops if cfg.loops != -1 else float("inf")
                while loop_idx < total_loops and not self._stop_event.is_set():
                    loop_idx += 1
                    self.log(f"开始第 {loop_idx} 轮")
                    for m in markers:
                        if self._stop_event.is_set():
                            break
                        self._mouse_ctrl.position = (m.x, m.y)
                        btn = MouseButton.left if m.button == "left" else MouseButton.right
                        self._mouse_ctrl.click(btn, 1)
                        self.log(f"点击 #{m.id} ({'左键' if m.button == 'left' else '右键'}) @ ({m.x}, {m.y})")
                        time.sleep(max(0.01, min(5.0, float(m.interval))))
                    self.log(f"结束第 {loop_idx} 轮")
                self.log("连点完成")
            finally:
                self._is_running = False
                if self.on_end:
                    self.on_end()

        threading.Thread(target=worker, daemon=True).start()
        return True


class ClickerPage(ttk.Frame):
    """“标记连点器”页面（来自 AC_new，适配为子页面：去掉自身的 GlobalHotKeys 注册，改由主程序统一管理）"""

    def __init__(self, master, on_hotkeys_changed: Callable[[], None]):
        super().__init__(master)
        self.root = self.winfo_toplevel()
        self.on_hotkeys_changed = on_hotkeys_changed

        # 数据
        self.markers: List[Marker] = []
        self.marker_windows: List[MarkerWindow] = []
        self.runner = ClickRunner(logger=self._log_safe, on_end=self._on_run_end)

        # 热键（可自定义，pynput 语法）
        self.hotkeys: Dict[str, str] = {
            "start_stop": "<f7>",
            "add_marker": "<f6>",
        }
        self._capture_action: Optional[str] = None
        self._cap_listener: Optional[KeyListener] = None
        self._cap_pressed: set = set()
        self._cap_label_vars: Dict[str, tk.StringVar] = {}
        self._hotkey_edit_buttons: List[ttk.Button] = []
        self._hotkey_status_var = tk.StringVar(value="")

        # 当前Tree编辑控件
        self._edit_widget: Optional[tk.Widget] = None
        self._edit_target: Optional[Tuple[str, str]] = None

        # UI
        self._build_ui()
        # self.center_on_screen()
        self.log("F7：开始/停止；F6：添加标记；方向键微调（Shift ×10）")

    # ---------- Helpers to integrate with Main ----------

    def get_hotkey_mapping(self) -> Dict[str, Callable[[], None]]:
        return {
            self.hotkeys["start_stop"]: lambda: self.after(0, self.toggle_start),
            self.hotkeys["add_marker"]: lambda: self.after(0, self.add_marker),
        }

    def get_all_hotkeys(self) -> List[str]:
        return list(self.hotkeys.values())

    def _notify_hotkeys_changed(self):
        if callable(self.on_hotkeys_changed):
            self.on_hotkeys_changed()

    # ---------- Window helpers ----------

    def center_on_screen(self):
        self.update_idletasks()
        w = self.winfo_width() or 460
        h = self.winfo_height() or 380
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -------------------- UI --------------------

    def _build_ui(self):
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True)

        # 左栏
        left = ttk.Frame(main, padding=5, width=180)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)

        # 参数
        param = ttk.LabelFrame(left, text="参数", padding=4)
        param.pack(fill=tk.X, pady=2)
        self.var_loops = tk.IntVar(value=1)
        self.var_delay = tk.DoubleVar(value=0.0)

        ttk.Label(param, text="循环(-1∞)").grid(row=0, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_loops, width=8).grid(row=0, column=1, sticky="e")
        ttk.Label(param, text="启动延迟(s)").grid(row=1, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_delay, width=8).grid(row=1, column=1, sticky="e")

        # 标记管理
        marks = ttk.LabelFrame(left, text="标记管理", padding=4)
        marks.pack(fill=tk.X, pady=2)
        ttk.Button(marks, text="添加", command=self.add_marker).pack(fill=tk.X, pady=1)
        ttk.Button(marks, text="删除选中", command=self.delete_selected).pack(fill=tk.X, pady=1)
        ttk.Button(marks, text="清空", command=self.clear_markers).pack(fill=tk.X, pady=1)

        # 操作
        ops = ttk.LabelFrame(left, text="操作", padding=4)
        ops.pack(fill=tk.X, pady=2)
        ttk.Button(ops, text="开始/停止", command=self.toggle_start).pack(fill=tk.X)

        # 配置（保存/加载本页的标记与参数）
        cfg = ttk.LabelFrame(left, text="配置", padding=4)
        cfg.pack(fill=tk.X, pady=2)
        ttk.Button(cfg, text="保存", command=self.save_config_file).pack(fill=tk.X, pady=1)
        ttk.Button(cfg, text="加载", command=self.load_config_file).pack(fill=tk.X, pady=1)

        # 右栏
        right = ttk.Frame(main)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab：标记
        tab_marks = ttk.Frame(self.notebook)
        self.notebook.add(tab_marks, text="标记")

        cols = ("id", "x", "y", "btn", "interval")
        self.tree = ttk.Treeview(tab_marks, columns=cols, show="headings", height=13)
        self.tree.heading("id", text="编号")
        self.tree.heading("x", text="X")
        self.tree.heading("y", text="Y")
        self.tree.heading("btn", text="按钮")
        self.tree.heading("interval", text="间隔(s)")
        self.tree.column("id", width=46, anchor="center")
        self.tree.column("x", width=50, anchor="center")
        self.tree.column("y", width=50, anchor="center")
        self.tree.column("btn", width=50, anchor="center")
        self.tree.column("interval", width=50, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 3))
        self.tree.bind("<KeyPress>", self._on_tree_key_press)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # Tab：快捷键设置
        tab_hotkey = ttk.Frame(self.notebook)
        self.notebook.add(tab_hotkey, text="快捷键设置")
        self._build_hotkey_tab(tab_hotkey)

        # Tab：使用说明
        tab_help = ttk.Frame(self.notebook)
        self.notebook.add(tab_help, text="使用说明")
        self._build_help_tab(tab_help)

        # Tab：日志
        tab_log = ttk.Frame(self.notebook)
        self.notebook.add(tab_log, text="日志")
        self.log_text = tk.Text(tab_log, wrap=tk.WORD, height=12, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _build_help_tab(self, parent: ttk.Frame):
        text = tk.Text(parent, wrap=tk.WORD, font=("微软雅黑", 10), spacing3=6)
        help_content = '''
📖 基本操作
⭐ 标记连点器的核心功能
➕ 添加标记：按钮 / F6
▶️ 开始/停止：按钮 / F7
🗑️ 删除标记：右键列表或屏幕标记

——
📝 列表编辑
📍 双击 X/Y 可直接修改坐标
📝 双击“按钮/间隔”可编辑：左键/右键、间隔时间

——
💡 小技巧
🎯 方向键移动坐标；↑↓←→ 键移动1px; 按住 Shift ×10px 步长
🔄 多标记将按列表顺序循环点击
        '''
        text.insert(tk.END, help_content)
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _build_hotkey_tab(self, parent: ttk.Frame):
        frame = ttk.Frame(parent, padding=6)
        frame.pack(fill=tk.BOTH, expand=True)
        rows = [("开始/停止", "start_stop"), ("添加标记", "add_marker")]
        for r, (label, keyname) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=4)
            var = tk.StringVar(value=self.hotkeys[keyname])
            self._cap_label_vars[keyname] = var
            ttk.Label(frame, textvariable=var).grid(row=r, column=1, sticky="w", padx=(0, 6))
            btn = ttk.Button(frame, text="修改", command=lambda k=keyname: self._begin_capture(k))
            btn.grid(row=r, column=2, sticky="e", padx=(6, 0))
            self._hotkey_edit_buttons.append(btn)

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_columnconfigure(2, weight=0)

        status = ttk.Label(frame, textvariable=self._hotkey_status_var, foreground="#0066cc",
                           wraplength=360, justify="left")
        status.grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ------------- Hotkey capture (shares logic with recorder page) -------------

    def _begin_capture(self, action: str):
        if self._capture_action is not None:
            messagebox.showinfo("快捷键", "已在设置其他快捷键，请先完成。")
            return
        self._capture_action = action

        # 暂停全局热键，避免与捕获冲突
        self.winfo_toplevel().stop_hotkeys()

        for b in self._hotkey_edit_buttons:
            b.state(["disabled"])
        self._hotkey_status_var.set("按下新组合键（Esc取消）")
        self._cap_label_vars[action].set(self.hotkeys[action] + "（等待输入…）")

        self.log(f"正在设置快捷键：{action}，请按下要使用的组合键…")

        self._cap_pressed = set()

        def is_modifier(k):
            return k in (Key.shift, Key.shift_l, Key.shift_r,
                         Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                         Key.alt, Key.alt_l, Key.alt_r,
                         Key.cmd, Key.cmd_l, Key.cmd_r)

        def mod_token(k):
            if k in (Key.shift, Key.shift_l, Key.shift_r): return "<shift>"
            if k in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):   return "<ctrl>"
            if k in (Key.alt, Key.alt_l, Key.alt_r):      return "<alt>"
            if k in (Key.cmd, Key.cmd_l, Key.cmd_r):      return "<cmd>"
            return None

        def keycode_to_char(k: KeyCode) -> Optional[str]:
            try:
                if isinstance(k, KeyCode):
                    if hasattr(k, "vk") and k.vk is not None:
                        vk = k.vk
                        if 0x30 <= vk <= 0x39:  # digits
                            return chr(vk)
                        if 0x41 <= vk <= 0x5A:  # letters
                            return chr(vk).lower()
                    if k.char and len(k.char) == 1 and k.char.isprintable():
                        return k.char.lower()
            except Exception:
                pass
            return None

        def on_press(key):
            if is_modifier(key):
                tok = mod_token(key)
                if tok:
                    self._cap_pressed.add(tok)
                return

            keystr = None
            if isinstance(key, KeyCode):
                keystr = keycode_to_char(key)
            elif isinstance(key, Key):
                name = str(key)
                if name.startswith("Key.f") and name[5:].isdigit():
                    keystr = f"<f{name[5:]}>"
            if keystr is None:
                return

            order = ["<ctrl>", "<alt>", "<shift>", "<cmd>"]
            mods = sorted(self._cap_pressed, key=lambda x: order.index(x) if x in order else 99)
            combo = "+".join(mods + [keystr]) if mods else keystr

            if self._is_hotkey_in_use(combo, exclude_action=self._capture_action):
                self.log(f"快捷键冲突：{combo} 已被其他页面/动作占用，请重试。")
                self._cap_pressed.clear()
                return

            self.hotkeys[self._capture_action] = combo
            self._cap_label_vars[self._capture_action].set(combo)
            self.log(f"已设置 {self._capture_action} 热键为：{combo}")
            self._end_capture()
            return False

        def on_release(key):
            if key in (Key.shift, Key.shift_l, Key.shift_r,
                       Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                       Key.alt, Key.alt_l, Key.alt_r,
                       Key.cmd, Key.cmd_l, Key.cmd_r):
                tok = mod_token(key)
                if tok and tok in self._cap_pressed:
                    self._cap_pressed.discard(tok)
                return
            if key == Key.esc:
                self.log("已取消设置热键。")
                self._end_capture()
                return False

        self._cap_listener = KeyListener(on_press=on_press, on_release=on_release)
        self._cap_listener.start()

    def _end_capture(self):
        try:
            if self._cap_listener:
                self._cap_listener.stop()
        except Exception:
            pass
        self._cap_listener = None
        self._cap_pressed.clear()

        if self._capture_action:
            self._cap_label_vars[self._capture_action].set(self.hotkeys[self._capture_action])
        self._hotkey_status_var.set("")
        for b in self._hotkey_edit_buttons:
            b.state(["!disabled"])

        self._capture_action = None
        # 通知主程序重新注册热键
        self._notify_hotkeys_changed()
        # 恢复全局热键
        self.winfo_toplevel().refresh_hotkeys()

    def _is_hotkey_in_use(self, combo: str, exclude_action: Optional[str] = None) -> bool:
        # 本页内部冲突
        for act, s in self.hotkeys.items():
            if act == exclude_action:
                continue
            if s == combo:
                return True
        # 其他页面
        other = self.winfo_toplevel().get_all_hotkeys(except_page=self)
        return combo in other

    # -------------------- Logging --------------------

    def _log_safe(self, msg: str):
        self.after(0, lambda: self.log(msg))

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # -------------------- Tree helpers / marker ops --------------------

    def _get_selected_ids(self) -> List[int]:
        return [int(self.tree.item(iid, "values")[0]) for iid in self.tree.selection()]

    def _refresh(self, preserve_ids: Optional[List[int]] = None):
        for idx, m in enumerate(self.markers, start=1):
            m.id = idx
        for idx, win in enumerate(self.marker_windows, start=1):
            win.update_number(idx)

        if preserve_ids is None:
            preserve_ids = self._get_selected_ids()

        self.tree.delete(*self.tree.get_children())
        for m in self.markers:
            self.tree.insert("", tk.END,
                             values=(m.id, m.x, m.y, "左键" if m.button == "left" else "右键", f"{m.interval:.2f}"))

        if preserve_ids:
            children = self.tree.get_children()
            for mid in preserve_ids:
                i = mid - 1
                if 0 <= i < len(children):
                    self.tree.selection_add(children[i])
            i0 = preserve_ids[0] - 1
            if 0 <= i0 < len(children):
                self.tree.focus(children[i0])

    def add_marker(self):
        mid = len(self.markers) + 1
        x = self.root.winfo_rootx() + self.root.winfo_width() - 90
        y = self.root.winfo_rooty() + 80
        m = Marker(id=mid, x=x, y=y, button="left", interval=0.2)
        self.markers.append(m)
        win = MarkerWindow(self.root, mid, self._on_marker_move, self._on_marker_close)
        win.geometry(f"+{x - win.SIZE // 2}+{y - win.SIZE // 2}")
        self.marker_windows.append(win)
        self._refresh()
        self.log(f"添加标记 #{mid}")

    def delete_selected(self):
        sel = self._get_selected_ids()
        if not sel:
            return
        for mid in sorted(sel, reverse=True):
            try:
                self.marker_windows[mid - 1].destroy()
            except Exception:
                pass
            del self.marker_windows[mid - 1]
            del self.markers[mid - 1]
        self._refresh()
        self.log(f"删除 {len(sel)} 个标记")

    def clear_markers(self):
        for w in self.marker_windows:
            try:
                w.destroy()
            except Exception:
                pass
        self.marker_windows.clear()
        self.markers.clear()
        self._refresh()
        self.log("清空标记")

    def _on_marker_move(self, mid, x, y):
        self.markers[mid - 1].x = x
        self.markers[mid - 1].y = y
        self._refresh(preserve_ids=[mid])
        self.tree.focus_set()

    def _on_marker_close(self, mid):
        try:
            self.marker_windows[mid - 1].destroy()
        except Exception:
            pass
        del self.marker_windows[mid - 1]
        del self.markers[mid - 1]
        self._refresh()
        self.log(f"删除标记 #{mid}")

    def _on_tree_select(self, _evt=None):
        self.tree.focus_set()

    def _on_tree_key_press(self, event):
        if event.keysym in ("Up", "Down", "Left", "Right"):
            step = 10 if (event.state & 0x0001) else 1  # Shift ×10
            dx = (-step if event.keysym == "Left" else (step if event.keysym == "Right" else 0))
            dy = (-step if event.keysym == "Up" else (step if event.keysym == "Down" else 0))
            sel_ids = self._get_selected_ids()
            if not sel_ids:
                return "break"

            for mid in sel_ids:
                idx = mid - 1
                self.markers[idx].x += dx
                self.markers[idx].y += dy
                w = self.marker_windows[idx]
                w.geometry(f"+{self.markers[idx].x - w.SIZE // 2}+{self.markers[idx].y - w.SIZE // 2}")

            self._refresh(preserve_ids=sel_ids)
            self.log(f"微调 {dx},{dy}" + (" [x10]" if step == 10 else ""))
            return "break"

    def _on_tree_double_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_map = {"#1": "id", "#2": "x", "#3": "y", "#4": "btn", "#5": "interval"}
        col_name = col_map.get(col_id)
        if col_name is None or col_name == "id":
            return

        bbox = self.tree.bbox(row_id, col_id)
        if not bbox:
            return
        x, y, w, h = bbox
        value = self.tree.set(row_id, col_name)

        self._close_editor(save=False)

        if col_name == "btn":
            widget = ttk.Combobox(self.tree, values=["左键", "右键"], state="readonly", width=max(6, w // 8))
            widget.set(value)
            widget.place(x=x, y=y, width=w, height=h)
            widget.focus_set()
            widget.bind("<<ComboboxSelected>>", lambda e: self._commit_editor(row_id, col_name, widget.get()))
            widget.bind("<FocusOut>", lambda e: self._commit_editor(row_id, col_name, widget.get()))
            self._edit_widget = widget
            self._edit_target = (row_id, col_name)
        else:
            widget = ttk.Entry(self.tree)
            widget.insert(0, value)
            widget.place(x=x, y=y, width=w, height=h)
            widget.focus_set()
            widget.select_range(0, tk.END)
            widget.bind("<Return>", lambda e: self._commit_editor(row_id, col_name, widget.get()))
            widget.bind("<Escape>", lambda e: self._close_editor(save=False))
            widget.bind("<FocusOut>", lambda e: self._commit_editor(row_id, col_name, widget.get()))
            self._edit_widget = widget
            self._edit_target = (row_id, col_name)

    def _commit_editor(self, row_id: str, col_name: str, new_val: str):
        try:
            idx = int(self.tree.set(row_id, "id")) - 1
            m = self.markers[idx]
            if col_name == "x":
                m.x = int(float(new_val))
                w = self.marker_windows[idx]
                w.geometry(f"+{m.x - w.SIZE // 2}+{m.y - w.SIZE // 2}")
            elif col_name == "y":
                m.y = int(float(new_val))
                w = self.marker_windows[idx]
                w.geometry(f"+{m.x - w.SIZE // 2}+{m.y - w.SIZE // 2}")
            elif col_name == "btn":
                m.button = "left" if new_val == "左键" else "right"
            elif col_name == "interval":
                iv = float(new_val)
                if iv < 0.01:
                    iv = 0.01
                m.interval = iv
        except Exception:
            pass
        finally:
            sel_ids = self._get_selected_ids()
            self._refresh(preserve_ids=sel_ids)
            self._close_editor(save=False)

    def _close_editor(self, save: bool = False):
        if self._edit_widget is not None:
            try:
                self._edit_widget.destroy()
            except Exception:
                pass
        self._edit_widget = None
        self._edit_target = None

    # -------------------- Run / Save / Load (page-level) --------------------

    def toggle_start(self):
        if self.runner.is_running():
            self.runner.stop()
        else:
            cfg = ClickConfig(self.var_loops.get(), self.var_delay.get())
            for w in self.marker_windows:
                w.withdraw()
            self.runner.start(self.markers.copy(), cfg)

    def _on_run_end(self):
        self.stop_play()
        for w in self.marker_windows:
            try:
                w.deiconify()
            except Exception:
                pass

    def save_config_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".json")
        if not path:
            return
        data = {
            "markers": [asdict(m) for m in self.markers],
            "config": {
                "loops": self.var_loops.get(),
                "delay": self.var_delay.get()
            },
            "hotkeys": self.hotkeys
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.log(f"配置已保存到 {path}")

    def load_config_file(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.clear_markers()
        cfg = data.get("config", {})
        self.var_loops.set(cfg.get("loops", 1))
        self.var_delay.set(cfg.get("delay", 0.0))
        hk = data.get("hotkeys", None)
        if hk:
            if hk.get("start_stop"):
                self.hotkeys["start_stop"] = hk["start_stop"]
            if hk.get("add_marker") and hk["add_marker"] != self.hotkeys["start_stop"]:
                self.hotkeys["add_marker"] = hk["add_marker"]
        self._notify_hotkeys_changed()

        for m in data.get("markers", []):
            self.add_marker()
            mm = self.markers[-1]
            mm.x = int(m.get("x", mm.x))
            mm.y = int(m.get("y", mm.y))
            mm.button = m.get("button", mm.button)
            mm.interval = float(m.get("interval", mm.interval))
            w = self.marker_windows[-1]
            w.geometry(f"+{mm.x - w.SIZE // 2}+{mm.y - w.SIZE // 2}")
        self._refresh()
        self.log(f"加载配置 {path}")

    # --------- Export / import state with global config ---------

    def export_state(self) -> dict:
        return {
            "hotkeys": dict(self.hotkeys),
            "params": {"loops": int(self.var_loops.get()), "delay": float(self.var_delay.get())},
            "markers": [asdict(m) for m in self.markers],
        }

    def import_state(self, data: dict):
        if not data:
            return
        hk = data.get("hotkeys", {})
        if hk:
            # 去重由主程序处理；此处直接接收
            self.hotkeys.update(hk)
        # 同步更新界面显示
        for keyname, val in self.hotkeys.items():
            if hasattr(self, '_cap_label_vars') and keyname in getattr(self, '_cap_label_vars', {}):
                self._cap_label_vars[keyname].set(val)
            if hasattr(self, '_cap_label_vars') is False and hasattr(self, '_cap_label_vars') == False:
                pass
        p = data.get("params", {})
        self.var_loops.set(int(p.get("loops", 1)))
        self.var_delay.set(float(p.get("delay", 0.0)))
        # markers
        self.clear_markers()
        for m in data.get("markers", []):
            self.add_marker()
            mm = self.markers[-1]
            mm.x = int(m.get("x", mm.x))
            mm.y = int(m.get("y", mm.y))
            mm.button = m.get("button", mm.button)
            mm.interval = float(m.get("interval", mm.interval))
            w = self.marker_windows[-1]
            w.geometry(f"+{mm.x - w.SIZE // 2}+{mm.y - w.SIZE // 2}")
        self._refresh()
        # 通知主程序更新热键注册
        self._notify_hotkeys_changed()


# =====================
# Recorder/Player Page
# =====================

@dataclass
class Event:
    t: float
    type: str
    x: int = None
    y: int = None
    dx: int = None
    dy: int = None
    button: str = None
    key: str = None


@dataclass
class Recording:
    events: List[Event]


class RecorderPlayer:
    def __init__(self, sample_interval=0.1, logger: Optional[Callable[[str], None]] = None,
                 on_play_end: Optional[Callable[[], None]] = None):
        self.sample_interval = sample_interval
        self.reset_recording()
        self._mouse_listener: Optional[mouse.Listener] = None
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._stop_record_event = threading.Event()
        self._stop_play_event = threading.Event()
        self._mouse_ctrl = mouse.Controller()
        self._keyboard_ctrl = keyboard.Controller()
        self.logger = logger
        self._is_playing = False
        self._on_play_end = on_play_end

    def log(self, msg: str):
        if self.logger:
            self.logger(msg)
        else:
            print(msg)

    def reset_recording(self):
        self.recording = Recording(events=[])
        self._start_time = None
        self._last_mouse_pos = None

    def _record_event(self, e: Event):
        self.recording.events.append(e)

    def _on_mouse_move(self, x, y):
        now = time.time() - self._start_time
        self._record_event(Event(t=now, type="mouse_move", x=int(x), y=int(y)))
        self._last_mouse_pos = (int(x), int(y))

    def _on_mouse_click(self, x, y, button, pressed):
        now = time.time() - self._start_time
        btn = str(button)
        etype = "mouse_down" if pressed else "mouse_up"
        self._record_event(Event(t=now, type=etype, x=int(x), y=int(y), button=btn))

    def _on_mouse_scroll(self, x, y, dx, dy):
        now = time.time() - self._start_time
        self._record_event(Event(t=now, type="mouse_scroll", x=int(x), y=int(y), dx=int(dx), dy=int(dy)))

    def _on_key_press(self, key):
        now = time.time() - self._start_time
        try:
            kstr = key.char if hasattr(key, 'char') and key.char is not None else f"Key.{key.name}"
        except Exception:
            kstr = str(key)
        self._record_event(Event(t=now, type="key_down", key=kstr))

    def _on_key_release(self, key):
        now = time.time() - self._start_time
        try:
            kstr = key.char if hasattr(key, 'char') and key.char is not None else f"Key.{key.name}"
        except Exception:
            kstr = str(key)
        self._record_event(Event(t=now, type="key_up", key=kstr))

    def start_recording(self, sample_interval: Optional[float] = None):
        if sample_interval is not None:
            self.sample_interval = sample_interval
        self.reset_recording()
        self._start_time = time.time()
        self._stop_record_event.clear()
        self.log("开始录制")

        self._mouse_listener = mouse.Listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll
        )
        self._keyboard_listener = keyboard.Listener(on_press=self._on_key_press, on_release=self._on_key_release)
        self._mouse_listener.start()
        self._keyboard_listener.start()

        def sampler():
            while not self._stop_record_event.is_set():
                if self._last_mouse_pos is not None:
                    now = time.time() - self._start_time
                    self._record_event(Event(t=now, type="mouse_move",
                                             x=self._last_mouse_pos[0], y=self._last_mouse_pos[1]))
                time.sleep(self.sample_interval)

        threading.Thread(target=sampler, daemon=True).start()
        return True, "开始录制"

    def stop_recording(self):
        self._stop_record_event.set()
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
        self.log(f"停止录制，共记录 {len(self.recording.events)} 个事件")
        return True, "停止录制"

    def _press_key(self, k: str):
        if k.startswith("Key."):
            try:
                key_obj = getattr(keyboard.Key, k.split(".", 1)[1])
                self._keyboard_ctrl.press(key_obj)
                return
            except Exception:
                pass
        try:
            self._keyboard_ctrl.press(k)
        except Exception:
            pass

    def _release_key(self, k: str):
        if k.startswith("Key."):
            try:
                key_obj = getattr(keyboard.Key, k.split(".", 1)[1])
                self._keyboard_ctrl.release(key_obj)
                return
            except Exception:
                pass
        try:
            self._keyboard_ctrl.release(k)
        except Exception:
            pass

    def is_recording(self) -> bool:
        return self._mouse_listener is not None

    def is_playing(self) -> bool:
        return self._is_playing

    def start_playback(self, speed: float = 1.0, loops: int = 1, gap: float = 0.0, delay: float = 0.0):
        if not self.recording.events:
            return False, "没有录制数据"
        if self._is_playing:
            return False, "回放已在进行中"
        self._stop_play_event.clear()
        events_copy = list(self.recording.events)
        self._is_playing = True
        self.log("开始回放")

        def play_loop():
            try:
                if delay > 0:
                    self.log(f"延迟 {delay} 秒开始回放")
                    time.sleep(delay)
                loop_index = 0
                total_loops = loops if loops != -1 else float('inf')
                while loop_index < total_loops and not self._stop_play_event.is_set():
                    loop_index += 1
                    prev_t = 0.0
                    self.log(f"回放 - 第 {loop_index} 轮开始")
                    for ev in events_copy:
                        if self._stop_play_event.is_set():
                            return
                        wait_time = (ev.t - prev_t) / max(speed, 1e-9)
                        time.sleep(wait_time)
                        if ev.type == "mouse_move":
                            self._mouse_ctrl.position = (ev.x, ev.y)
                        elif ev.type == "mouse_down":
                            btn = mouse.Button.left if "left" in ev.button else mouse.Button.right
                            self._mouse_ctrl.press(btn)
                        elif ev.type == "mouse_up":
                            btn = mouse.Button.left if "left" in ev.button else mouse.Button.right
                            self._mouse_ctrl.release(btn)
                        elif ev.type == "mouse_scroll":
                            self._mouse_ctrl.position = (ev.x, ev.y)
                            self._mouse_ctrl.scroll(ev.dx, ev.dy)
                        elif ev.type == "key_down":
                            self._press_key(ev.key)
                        elif ev.type == "key_up":
                            self._release_key(ev.key)
                        prev_t = ev.t
                    self.log(f"回放 - 第 {loop_index} 轮结束")
                    if gap > 0:
                        time.sleep(gap)
                self.log("回放完成")
            finally:
                self._is_playing = False
                if self._on_play_end:
                    self._on_play_end()

        threading.Thread(target=play_loop, daemon=True).start()
        return True, "开始回放"

    def stop_playback(self):
        self._stop_play_event.set()
        return True, "停止回放"

    def save_to_file(self, path: str):
        data = [asdict(e) for e in self.recording.events]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.log(f"保存录制到 {path}")

    def load_from_file(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.recording.events = [Event(**e) for e in data]
        self.log(f"从 {path} 加载录制，共 {len(self.recording.events)} 个事件")


class RecorderPage(ttk.Frame):
    """“操作录制/回放”页面（来自 ACv2_0_0，适配为子页面）"""

    def __init__(self, master, on_hotkeys_changed: Callable[[], None]):
        super().__init__(master)
        self.root = master
        self.on_hotkeys_changed = on_hotkeys_changed

        self.current_file: Optional[str] = None
        self.recorder = RecorderPlayer(sample_interval=0.1, logger=self._log_safe, on_play_end=self._on_play_end)

        self.hotkeys: Dict[str, str] = {
            "toggle_record": "<f9>",
            "toggle_play": "<f10>",
        }
        self._capture_action: Optional[str] = None
        self._cap_listener: Optional[KeyListener] = None
        self._cap_pressed: set = set()
        self._cap_label_vars: Dict[str, tk.StringVar] = {}
        self._hotkey_edit_buttons: List[ttk.Button] = []
        self._hotkey_status_var = tk.StringVar(value="")

        self._build_ui()

        self.log("热键：F9=录制切换；F10=回放切换")

    # ---------- Integrations ----------
    def get_hotkey_mapping(self) -> Dict[str, Callable[[], None]]:
        return {
            self.hotkeys["toggle_record"]: lambda: self.after(0, self.toggle_record),
            self.hotkeys["toggle_play"]: lambda: self.after(0, self.toggle_play),
        }

    def get_all_hotkeys(self) -> List[str]:
        return list(self.hotkeys.values())

    def _notify_hotkeys_changed(self):
        if callable(self.on_hotkeys_changed):
            self.on_hotkeys_changed()

    # -------------------- UI --------------------

    def _build_ui(self):
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main, padding=5, width=180)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)

        param = ttk.LabelFrame(left, text="参数", padding=4)
        param.pack(fill=tk.X, pady=2)
        self.var_interval = tk.DoubleVar(value=0.1)
        self.var_speed = tk.DoubleVar(value=1.0)
        self.var_loops = tk.IntVar(value=1)
        self.var_gap = tk.DoubleVar(value=0.0)
        self.var_delay = tk.DoubleVar(value=0.0)

        ttk.Label(param, text="采样间隔(s)").grid(row=0, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_interval, width=8).grid(row=0, column=1, sticky="e")
        ttk.Label(param, text="回放倍速").grid(row=1, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_speed, width=8).grid(row=1, column=1, sticky="e")
        ttk.Label(param, text="循环(-1∞)").grid(row=2, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_loops, width=8).grid(row=2, column=1, sticky="e")
        ttk.Label(param, text="循环间隔(s)").grid(row=3, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_gap, width=8).grid(row=3, column=1, sticky="e")
        ttk.Label(param, text="启动延迟(s)").grid(row=4, column=0, sticky="w")
        ttk.Entry(param, textvariable=self.var_delay, width=8).grid(row=4, column=1, sticky="e")

        ops = ttk.LabelFrame(left, text="操作", padding=4)
        ops.pack(fill=tk.X, pady=2)
        self.btn_record = ttk.Button(ops, text="录制：开始/停止", command=self.toggle_record)
        self.btn_record.pack(fill=tk.X, pady=1)
        self.btn_play = ttk.Button(ops, text="回放：开始/停止", command=self.toggle_play)
        self.btn_play.pack(fill=tk.X, pady=1)

        cfg = ttk.LabelFrame(left, text="配置", padding=4)
        cfg.pack(fill=tk.X, pady=2)
        ttk.Button(cfg, text="保存", command=self.save_record).pack(fill=tk.X, pady=1)
        ttk.Button(cfg, text="加载", command=self.load_record).pack(fill=tk.X, pady=1)

        right = ttk.Frame(main)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_log = ttk.Frame(self.notebook)
        self.notebook.add(tab_log, text="日志")
        self.log_text = tk.Text(tab_log, wrap=tk.WORD, height=12, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        tab_hotkey = ttk.Frame(self.notebook)
        self.notebook.add(tab_hotkey, text="快捷键设置")
        self._build_hotkey_tab(tab_hotkey)

        tab_help = ttk.Frame(self.notebook)
        self.notebook.add(tab_help, text="使用说明")
        self._build_help_tab(tab_help)

    def _build_help_tab(self, parent: ttk.Frame):
        text = tk.Text(parent, wrap=tk.WORD, font=("微软雅黑", 10), spacing3=6)
        help_content = '''
📖 基本操作
⭐ 录制与回放是核心功能
🎬 录制：按钮 / F9
🔁 回放：按钮 / F10
💾 保存 / 📂 加载：导出或导入 JSON 文件

——
⚙️ 参数设置
⏱️ 采样间隔：0.01 ~ 1 秒
⚡ 回放倍速：>1 加速，<1 减速
🔂 循环次数：-1 = 无限循环
⏳ 循环间隔：两轮之间等待
⌛ 启动延迟：回放前等待时间

——
💡 小技巧
🖱️ 录制会采集：鼠标移动 / 点击 / 滚轮 / 键盘操作
⌨️ 热键：F9 开始/停止录制；F10 开始/停止回放
⚠️ 正在录制时 → 回放按钮禁用
⚠️ 正在回放时 → 录制按钮禁用
        '''
        text.insert(tk.END, help_content)
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _build_hotkey_tab(self, parent: ttk.Frame):
        frame = ttk.Frame(parent, padding=6)
        frame.pack(fill=tk.BOTH, expand=True)

        rows = [("录制切换", "toggle_record"), ("回放切换", "toggle_play")]
        for r, (label, keyname) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=4)
            var = tk.StringVar(value=self.hotkeys[keyname])
            self._cap_label_vars[keyname] = var
            ttk.Label(frame, textvariable=var).grid(row=r, column=1, sticky="w", padx=(0, 6))
            btn = ttk.Button(frame, text="修改", command=lambda k=keyname: self._begin_capture(k))
            btn.grid(row=r, column=2, sticky="e", padx=(6, 0))
            self._hotkey_edit_buttons.append(btn)

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_columnconfigure(2, weight=0)

        status = ttk.Label(frame, textvariable=self._hotkey_status_var,
                           foreground="#0066cc", wraplength=360, justify="left")
        status.grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(10, 0))

    # Hotkey capture

    def _begin_capture(self, action: str):
        if self._capture_action is not None:
            messagebox.showinfo("快捷键", "已在设置其他快捷键，请先完成。")
            return
        self._capture_action = action

        # 暂停全局热键
        self.winfo_toplevel().stop_hotkeys()

        for b in self._hotkey_edit_buttons:
            b.state(["disabled"])
        self._hotkey_status_var.set("按下新组合键（Esc取消）")
        self._cap_label_vars[action].set(self.hotkeys[action] + "（等待输入…）")

        self.log(f"正在设置快捷键：{action}，请按下要使用的组合键…")

        self._cap_pressed = set()

        def is_modifier(k):
            return k in (Key.shift, Key.shift_l, Key.shift_r,
                         Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                         Key.alt, Key.alt_l, Key.alt_r,
                         Key.cmd, Key.cmd_l, Key.cmd_r)

        def mod_token(k):
            if k in (Key.shift, Key.shift_l, Key.shift_r): return "<shift>"
            if k in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):   return "<ctrl>"
            if k in (Key.alt, Key.alt_l, Key.alt_r):      return "<alt>"
            if k in (Key.cmd, Key.cmd_l, Key.cmd_r):      return "<cmd>"
            return None

        def keycode_to_char(k: KeyCode) -> Optional[str]:
            try:
                if isinstance(k, KeyCode):
                    if hasattr(k, "vk") and k.vk is not None:
                        vk = k.vk
                        if 0x30 <= vk <= 0x39:
                            return chr(vk)
                        if 0x41 <= vk <= 0x5A:
                            return chr(vk).lower()
                    if k.char and len(k.char) == 1 and k.char.isprintable():
                        return k.char.lower()
            except Exception:
                pass
            return None

        def on_press(key):
            if is_modifier(key):
                tok = mod_token(key)
                if tok:
                    self._cap_pressed.add(tok)
                return

            keystr = None
            if isinstance(key, KeyCode):
                keystr = keycode_to_char(key)
            elif isinstance(key, Key):
                name = str(key)
                if name.startswith("Key.f") and name[5:].isdigit():
                    keystr = f"<f{name[5:]}>"
            if keystr is None:
                return

            order = ["<ctrl>", "<alt>", "<shift>", "<cmd>"]
            mods = sorted(self._cap_pressed, key=lambda x: order.index(x) if x in order else 99)
            combo = "+".join(mods + [keystr]) if mods else keystr

            if self._is_hotkey_in_use(combo, exclude_action=self._capture_action):
                self.log(f"快捷键冲突：{combo} 已被其他页面/动作占用，请重试。")
                self._cap_pressed.clear()
                return

            self.hotkeys[self._capture_action] = combo
            self._cap_label_vars[self._capture_action].set(combo)
            self.log(f"已设置 {self._capture_action} 热键为：{combo}")
            self._end_capture()
            return False

        def on_release(key):
            if key in (Key.shift, Key.shift_l, Key.shift_r,
                       Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                       Key.alt, Key.alt_l, Key.alt_r,
                       Key.cmd, Key.cmd_l, Key.cmd_r):
                tok = mod_token(key)
                if tok and tok in self._cap_pressed:
                    self._cap_pressed.discard(tok)
                return
            if key == Key.esc:
                self.log("已取消设置热键。")
                self._end_capture()
                return False

        self._cap_listener = KeyListener(on_press=on_press, on_release=on_release)
        self._cap_listener.start()

    def _end_capture(self):
        try:
            if self._cap_listener:
                self._cap_listener.stop()
        except Exception:
            pass
        self._cap_listener = None
        self._cap_pressed.clear()

        if self._capture_action:
            self._cap_label_vars[self._capture_action].set(self.hotkeys[self._capture_action])
        self._hotkey_status_var.set("")
        for b in self._hotkey_edit_buttons:
            b.state(["!disabled"])

        self._capture_action = None
        self._notify_hotkeys_changed()
        self.winfo_toplevel().refresh_hotkeys()

    def _is_hotkey_in_use(self, combo: str, exclude_action: Optional[str] = None) -> bool:
        for act, s in self.hotkeys.items():
            if act == exclude_action:
                continue
            if s == combo:
                return True
        other = self.winfo_toplevel().get_all_hotkeys(except_page=self)
        return combo in other

    # -------------------- Logging --------------------

    def _log_safe(self, msg: str):
        self.after(0, lambda: self.log(msg))

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # -------------------- Actions --------------------

    def toggle_record(self):
        if self.recorder.is_playing():
            return
        if self.recorder.is_recording():
            self.stop_record()
        else:
            self.start_record()

    def toggle_play(self):
        if self.recorder.is_recording():
            return
        if self.recorder.is_playing():
            self.stop_play()
        else:
            self.start_play()

    def start_record(self):
        interval = max(0.01, min(1.0, float(self.var_interval.get())))
        self.var_interval.set(interval)
        ok, msg = self.recorder.start_recording(sample_interval=interval)
        if ok:
            self.btn_play.state(['disabled'])
        self.log(msg)

    def stop_record(self):
        ok, msg = self.recorder.stop_recording()
        self.btn_play.state(['!disabled'])
        self.log(msg)

    def start_play(self):
        ok, msg = self.recorder.start_playback(
            speed=float(self.var_speed.get()),
            loops=int(self.var_loops.get()),
            gap=float(self.var_gap.get()),
            delay=float(self.var_delay.get())
        )
        if ok:
            self.btn_record.state(['disabled'])
        self.log(msg)

    def stop_play(self):
        ok, msg = self.recorder.stop_playback()
        self.btn_record.state(['!disabled'])
        self.log(msg)

    def _on_play_end(self):
        # 回放线程自然结束时，恢复按钮状态（启用“录制”）
        try:
            self.after(0, self.stop_play)
        except Exception:
            # Fallback：直接调用
            self.stop_play()
        self.after(0, lambda: self.log("回放线程结束"))

    # -------------------- Save / Load --------------------

    def save_record(self):
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON files", "*.json")])
        if not path:
            return
        self.recorder.save_to_file(path)
        self.current_file = path

    def load_record(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not path:
            return
        self.recorder.load_from_file(path)
        self.current_file = path

    # --------- Export / import state with global config ---------

    def export_state(self) -> dict:
        return {
            "hotkeys": dict(self.hotkeys),
            "params": {
                "interval": float(self.var_interval.get()),
                "speed": float(self.var_speed.get()),
                "loops": int(self.var_loops.get()),
                "gap": float(self.var_gap.get()),
                "delay": float(self.var_delay.get()),
            },
            "last_file": self.current_file,
        }

    def import_state(self, data: dict):
        if not data:
            return
        hk = data.get("hotkeys", {})
        if hk:
            self.hotkeys.update(hk)
        # 同步更新界面显示
        for keyname, val in self.hotkeys.items():
            if hasattr(self, '_cap_label_vars') and keyname in getattr(self, '_cap_label_vars', {}):
                self._cap_label_vars[keyname].set(val)
            if hasattr(self, '_cap_label_vars') is False and hasattr(self, '_cap_label_vars') == False:
                pass
        p = data.get("params", {})
        self.var_interval.set(float(p.get("interval", 0.1)))
        self.var_speed.set(float(p.get("speed", 1.0)))
        self.var_loops.set(int(p.get("loops", 1)))
        self.var_gap.set(float(p.get("gap", 0.0)))
        self.var_delay.set(float(p.get("delay", 0.0)))
        self.current_file = data.get("last_file", None)
        self._notify_hotkeys_changed()


# ===================
# Main Application
# ===================

class MainApp(tk.Tk):
    def __init__(self, config_path: Optional[str] = None):
        super().__init__()
        self.withdraw()

        self.title(APP_TITLE)
        self.resizable(False, False)

        try:
            self.iconbitmap(APP_ICON)
        except Exception:
            pass

        try:
            _wh = DEFAULT_GEOMETRY.split("+", 1)[0]
            _w, _h = _wh.split("x")
            w, h = int(_w), int(_h)
        except Exception:
            w, h = 460, 400  # 兜底
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.config_mgr = ConfigManager(config_path)
        self.config_mgr.load()

        # UI: top-level notebook with two pages
        container = ttk.Notebook(self)
        container.pack(fill=tk.BOTH, expand=True)

        self.hotkey_mgr = GlobalHotkeyManager(on_error=lambda msg: messagebox.showwarning("热键错误", msg))

        # create pages
        self.page_rec = RecorderPage(container, on_hotkeys_changed=self.refresh_hotkeys)
        self.page_clk = ClickerPage(container, on_hotkeys_changed=self.refresh_hotkeys)

        container.add(self.page_rec, text="操作录制/回放")
        container.add(self.page_clk, text="标记连点器")
        self.container = container

        # Load config into pages
        self.import_config(self.config_mgr.data)

        # 默认启动显示“操作录制/回放”页面（始终选中第一个标签页）
        self.container.select(0)

        # Register hotkeys (ensure uniqueness)
        self.ensure_no_duplicates_on_load()
        self.refresh_hotkeys()

        # 右键菜单
        self._context_menu = tk.Menu(self, tearoff=0)
        self._context_menu.add_command(label="清除配置", command=self.reset_config)
        self._context_menu.add_command(label="开启置顶", command=self.toggle_topmost)
        self._topmost_index = self._context_menu.index("end")
        self.bind("<Button-3>", self._show_context_menu)

        # WM close
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.deiconify()

    # ---------- Hotkey management ----------

    def stop_hotkeys(self):
        self.hotkey_mgr.stop()

    def refresh_hotkeys(self):
        # Build combined mapping
        mapping = {}
        for combo, cb in self.page_rec.get_hotkey_mapping().items():
            mapping[combo] = cb
        for combo, cb in self.page_clk.get_hotkey_mapping().items():
            if combo in mapping:
                # Should not happen because we prevent duplicates on setting;
                # but just in case, skip clicker duplicate and warn.
                print("检测到重复热键：", combo, "将忽略。")
                continue
            mapping[combo] = cb
        self.hotkey_mgr.set_mapping(mapping)
        # --- 持久化热键到配置文件 ---
        try:
            self.config_mgr.data = self.export_config()
            # 保存 last_tab 以便下次使用，但启动时仍会强制显示录制/回放页
            self.config_mgr.save()
        except Exception as e:
            print("保存配置失败：", e)

    def get_all_hotkeys(self, except_page=None) -> set:
        s = set()
        if except_page is not self.page_rec:
            s.update(self.page_rec.get_all_hotkeys())
        if except_page is not self.page_clk:
            s.update(self.page_clk.get_all_hotkeys())
        return s

    # ---------- Config import/export ----------

    def import_config(self, data: dict):
        if not data:
            return
        # recorder
        self.page_rec.import_state(data.get("recorder", {}))
        # clicker
        self.page_clk.import_state(data.get("clicker", {}))

    def export_config(self) -> dict:
        data = default_config()
        data["global"]["last_tab"] = int(self.container.index(self.container.select()))
        data["recorder"] = self.page_rec.export_state()
        data["clicker"] = self.page_clk.export_state()
        return data

    def ensure_no_duplicates_on_load(self):
        # If duplicate combos exist across pages after loading config, auto-fix by shifting recorder keys up to free F-keys.
        used = set()

        def add_or_fix(combo: str, preferred_pool: List[str]) -> str:
            if combo not in used:
                used.add(combo)
                return combo
            # find free in pool
            for c in preferred_pool:
                if c not in used:
                    used.add(c)
                    return c
            # fallback: ctrl+alt+letter options
            for c in ["<ctrl>+<alt>+r>", "<ctrl>+<alt>+p>", "<ctrl>+<alt>+m>"]:
                if c not in used:
                    used.add(c)
                    return c
            return combo  # give up

        # prefer defaults: clicker(F6,F7), recorder(F9,F10)
        pools_clk = ["<f6>", "<f7>", "<f8>", "<f9>", "<f10>", "<f11>", "<f12>"]
        pools_rec = ["<f9>", "<f10>", "<f11>", "<f12>", "<f6>", "<f7>", "<f8>"]

        # clicker first
        hk_clk = self.page_clk.hotkeys
        hk_clk["add_marker"] = add_or_fix(hk_clk.get("add_marker", "<f6>"), pools_clk)
        hk_clk["start_stop"] = add_or_fix(hk_clk.get("start_stop", "<f7>"), pools_clk)

        # recorder next
        hk_rec = self.page_rec.hotkeys
        hk_rec["toggle_record"] = add_or_fix(hk_rec.get("toggle_record", "<f9>"), pools_rec)
        hk_rec["toggle_play"] = add_or_fix(hk_rec.get("toggle_play", "<f10>"), pools_rec)

    def _show_context_menu(self, event):
        # 根据当前置顶状态更新菜单文字
        if self.attributes("-topmost"):
            self._context_menu.entryconfig(self._topmost_index, label="关闭置顶")
        else:
            self._context_menu.entryconfig(self._topmost_index, label="开启置顶")

        self._context_menu.tk_popup(event.x_root, event.y_root)

    def reset_config(self):
        if messagebox.askyesno("确认", "确定要清除配置并恢复默认吗？"):
            self.config_mgr.data = default_config()
            self.import_config(self.config_mgr.data)
            self.refresh_hotkeys()
            self.config_mgr.save()
            messagebox.showinfo("提示", "配置已恢复为默认值")

    def toggle_topmost(self):
        current = self.attributes("-topmost")
        self.attributes("-topmost", not current)

    # ---------- Close ----------

    def on_closing(self):
        # remember last tab
        self.config_mgr.data = self.export_config()
        # save
        self.config_mgr.save()

        # stop hotkeys and children
        try:
            self.stop_hotkeys()
        except Exception:
            pass
        try:
            self.page_rec.recorder.stop_playback()
            self.page_rec.recorder.stop_recording()
        except Exception:
            pass
        try:
            self.page_clk.runner.stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
