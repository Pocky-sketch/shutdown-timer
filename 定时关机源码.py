# -*- coding: utf-8 -*-
"""定时关机 —— 粉色 tkinter 定时关机小工具 (Windows)

用法: pythonw 定时关机源码.py   (或打包后的 定时关机.exe)

保险机制(三段):
  1) 到点先看键鼠是否空闲 → 空闲才关机(留 60 秒反悔窗口)
  2) 正在用电脑 → 顺延 5 分钟后重新检查
  3) 全程给 OS 布一个"名义时长 + 5 分钟"的兜底定时, 程序崩了/被关了也照样关

设计取舍: 关闭窗口 = 结束本程序(省资源), OS 定时仍在跑;
         下次打开会自动认出"上次的定时还在进行中", 可点取消关机撤销。
"""
import tkinter as tk
import subprocess
import math
import ctypes
import os
import sys
import json
import time
import tempfile
from ctypes import wintypes


# ── 保险机制参数 ──
IDLE_THRESHOLD = 300      # 空闲判定: 距上次输入 ≥ 5 分钟 = 没人用
POSTPONE_SECONDS = 300    # 使用中顺延: 5 分钟后再检查
FINAL_GRACE = 60          # 判定空闲后: 留 60 秒反悔窗口
FAILSAFE_PAD = 300        # OS 兜底定时比名义时长多 5 分钟(程序崩了也照样关)

# 图标(与脚本/exe 同目录, 不存在则跳过图标, 不影响程序运行)
# 打包成 exe 后 __file__ 指向临时解包目录, 得改用 exe 自己所在目录
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(_BASE_DIR, "定时关机图标.ico")

# 单实例互斥体名 / 窗口标题(用于二次启动时把已有窗口拉到前台)
MUTEX_NAME = "ShutdownTimer_SingleInstance_PockySketch"
WINDOW_TITLE = "定时关机"

# CREATE_NO_WINDOW: 无控制台进程(pythonw/exe)调 shutdown.exe 时,
# 不加这个标志 Windows 会为子进程新建终端窗口 => 每次调用都弹一下终端窗,
# 而且要多等约 0.8 秒。加上后不弹窗, 耗时降到几十毫秒。
CREATE_NO_WINDOW = 0x08000000

ERROR_ALREADY_EXISTS = 183


def shutdown_cmd(*args):
    """所有 shutdown 调用的唯一出口, 保证不弹终端窗口"""
    return subprocess.run(["shutdown", *args],
                          capture_output=True,
                          creationflags=CREATE_NO_WINDOW)


def state_path():
    """上次设定的定时状态(判断"重开是否还有活着的定时")"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "ShutdownTimer", "state.json")


def uptime_seconds():
    """本次开机已运行秒数"""
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return ctypes.windll.kernel32.GetTickCount64() / 1000.0


def boot_epoch():
    """本次开机的 Unix 时间戳: 状态文件跨开机即作废"""
    return time.time() - uptime_seconds()


class ShutdownTimer:
    def __init__(self, single_instance=True, restore_state=True):
        self._mutex = None
        if single_instance and not self.acquire_single_instance():
            self.focus_existing_window()
            raise SystemExit(0)

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("420x500")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 时间源: 计时靠"截止时刻"反算, 不用秒数自加(会漂移)
        self._now = time.monotonic
        self.deadline = None

        self.init_palette()
        self.apply_icon()
        self.build_ui()

        self.timer_running = False
        self.remaining_seconds = 0
        self.after_id = None
        self.final_grace = False   # 已判定空闲, 进入最终关机倒计时
        self.in_postpone = False   # 处于"检测到使用中"的顺延循环

        self.root.configure(bg=self.BG)
        self.update_ui()
        self.update_countdown()
        if restore_state:
            self.restore_pending_state()

        # 居中
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"+{x}+{y}")

        self.root.mainloop()

    # ═══════════════════════════════════════
    #  单实例 / 状态文件
    # ═══════════════════════════════════════

    def acquire_single_instance(self):
        """拿到互斥体 = 本程序唯一实例; 拿不到说明已经开着一个"""
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    @staticmethod
    def focus_existing_window():
        """第二个实例: 把已经开着的窗口拉到前台, 自己退出"""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def save_state(self):
        try:
            path = state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "boot_epoch": boot_epoch(),
                    "deadline": time.time() + max(0, self.deadline - self._now()),
                    "protect": bool(self.protect_var.get()),
                    "total": self.total_seconds,
                    "phase": ("grace" if self.final_grace else
                              "postpone" if self.in_postpone else "counting"),
                }, f)
        except Exception:
            pass

    def clear_state(self):
        try:
            os.remove(state_path())
        except OSError:
            pass

    def restore_pending_state(self):
        """重开时认出上次还没到点的定时 —— 让"再次打开可以取消定时"看得见"""
        try:
            with open(state_path(), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            self.clear_state()
            return
        left = st.get("deadline", 0) - time.time()
        # 同一次开机的判定要留容差: boot_epoch 由"当前时间 - 开机秒数"算得,
        # 两次计算之间有毫秒级误差, 直接 != 比较会永远判定为"跨开机"
        same_boot = abs(st.get("boot_epoch", 0) - boot_epoch()) < 30
        if not same_boot or left <= 0:
            # 跨了开机(说明已经关过机) 或 早就过点 → 作废
            self.clear_state()
            return
        self.protect_var.set(bool(st.get("protect", True)))
        self.total_seconds = int(st.get("total", 0))
        self.deadline = self._now() + left
        self.remaining_seconds = max(1, int(math.ceil(left)))
        self.final_grace = st.get("phase") == "grace"
        self.in_postpone = st.get("phase") == "postpone"
        self.timer_running = True
        self.update_ui()
        self.update_countdown()
        self.status_var.set("♻ 上次设定的关机仍在进行中，点「取消关机」可撤销")
        self.status_label.config(fg=self.STATUS_ACTIVE)
        self.after_id = self.root.after(1000, self.tick)

    # ═══════════════════════════════════════
    #  配色 / 图标
    # ═══════════════════════════════════════

    def init_palette(self):
        # ── 粉色少女系配色 ──
        self.BG          = "#FFF0F5"   # 薰衣草腮红底
        self.CARD        = "#FFFAFD"   # 卡片白
        self.TITLE       = "#D44A7A"   # 深玫瑰标题
        self.SUB         = "#C78B9E"   # 灰粉副文本
        self.ACCENT      = "#F08FB4"   # 主按钮粉
        self.ACCENT2     = "#E8759E"   # 按钮 hover
        self.CANCEL_BG   = "#FFB3B3"   # 取消按钮
        self.CANCEL_HOVER = "#FF9E9E"  # 取消按钮 hover
        self.INPUT_BG    = "#FFF8FA"   # 输入框底
        self.INPUT_BORDER = "#F0C0D0"  # 输入框边
        self.TEXT_ON_PINK = "#FFFFFF"  # 粉底白字
        self.LACE        = "#FFD1DC"   # 蕾丝粉
        self.LACE_DARK   = "#F0B8C8"   # 蕾丝深粉
        self.STATUS_IDLE = "#C78B9E"
        self.STATUS_ACTIVE = "#E8759E"
        self.STATUS_WARN = "#E8A45A"
        self.STATUS_ALERT = "#FF6B8A"

    def apply_icon(self):
        """窗口 + 任务栏图标都换成自定义 ico。图标缺失只跳过图标, 程序照常运行。"""
        if not os.path.isfile(ICON_PATH):
            return
        # 标题栏图标 (失败不影响下面的任务栏图标)
        try:
            self.root.iconbitmap(ICON_PATH)
        except tk.TclError:
            pass
        # 任务栏图标: WM_SETICON 打到自己的真实顶层 HWND。
        # 用 GetParent(winfo_id) 而不是 FindWindowW 按标题找——
        # 多实例并存时 FindWindowW 会打到别的窗口, 自己反而没图标
        # (任务栏显示 AUMID 未关联的通用占位图)。
        try:
            user32 = ctypes.windll.user32
            user32.GetParent.restype = wintypes.HWND
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                          wintypes.UINT, ctypes.c_int,
                                          ctypes.c_int, wintypes.UINT]
            user32.SendMessageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            hwnd = user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            WM_SETICON = 0x0080
            LR_LOADFROMFILE = 0x10
            # 16px 小图标(任务栏/标题栏) + 32px 大图标(Alt+Tab)
            for size, icon_type in ((16, 0), (32, 1)):
                hicon = user32.LoadImageW(None, ICON_PATH, 1,
                                          size, size, LR_LOADFROMFILE)
                if hicon:
                    user32.SendMessageW(hwnd, WM_SETICON,
                                        icon_type, hicon)
        except Exception:
            pass

    # ═══════════════════════════════════════
    #  蕾丝边绘制
    # ═══════════════════════════════════════

    def draw_lace_top(self, canvas):
        """顶部蕾丝花边"""
        w = 420
        h = 56
        # 主体横条
        canvas.create_rectangle(0, 16, w, h, fill=self.LACE, outline="", tags="lace")

        # 上层波浪小圆
        for i in range(0, w + 18, 18):
            y = 16
            r = 10
            canvas.create_oval(i - r, y - r, i + r, y + r,
                               fill=self.LACE, outline=self.LACE_DARK, width=1, tags="lace")
            # 小圆内的小亮点
            canvas.create_oval(i - 3, y - 5, i + 3, y + 1,
                               fill=self.LACE_DARK, outline="", tags="lace")

        # 下层波浪半圆（扇形蕾丝）
        for i in range(9, w + 18, 36):
            x, y = i, h
            r = 14
            canvas.create_arc(x - r, y - r, x + r, y + r,
                              start=0, extent=180, style="chord",
                              fill=self.LACE, outline=self.LACE_DARK, width=1, tags="lace")

        # 小花点缀
        for i in range(27, w, 36):
            self.draw_mini_flower(canvas, i, 8)

    def draw_lace_bottom(self, canvas):
        """底部蕾丝花边"""
        w = 420
        h = 56
        canvas.create_rectangle(0, 0, w, 40, fill=self.LACE, outline="", tags="lace")

        # 上半波浪（倒挂扇贝）
        for i in range(9, w + 18, 36):
            x, y = 0, 0
            r = 14
            canvas.create_arc(i - r, -r, i + r, r,
                              start=0, extent=-180, style="chord",
                              fill=self.LACE, outline=self.LACE_DARK, width=1, tags="lace")

        # 下沿小圆
        for i in range(0, w + 18, 18):
            y = 40
            r = 10
            canvas.create_oval(i - r, y - r, i + r, y + r,
                               fill=self.LACE, outline=self.LACE_DARK, width=1, tags="lace")
            canvas.create_oval(i - 3, y - 1, i + 3, y + 5,
                               fill=self.LACE_DARK, outline="", tags="lace")

        # 小花
        for i in range(27, w, 36):
            self.draw_mini_flower(canvas, i, 48)

    def draw_mini_flower(self, canvas, cx, cy, size=5):
        """五瓣小花"""
        r = size
        for j in range(5):
            angle = math.radians(j * 72 - 90)
            px = cx + r * math.cos(angle)
            py = cy + r * math.sin(angle)
            s = r * 0.6
            canvas.create_oval(px - s, py - s, px + s, py + s,
                               fill="#FFF", outline=self.LACE_DARK, width=1, tags="lace")
        # 花心
        canvas.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5,
                           fill="#FFF5F5", outline="", tags="lace")

    # ═══════════════════════════════════════
    #  UI 布局
    # ═══════════════════════════════════════

    def build_ui(self):
        # ── 顶部蕾丝 ──
        top_lace = tk.Canvas(self.root, width=420, height=56,
                             bg=self.BG, highlightthickness=0, bd=0)
        top_lace.pack()
        self.draw_lace_top(top_lace)

        # ── 蝴蝶结标题 ──
        title_frame = tk.Frame(self.root, bg=self.BG)
        title_frame.pack(pady=(6, 0))

        tk.Label(title_frame, text="🎀", font=("Segoe UI Symbol", 18),
                 bg=self.BG).pack(side="left", padx=(0, 4))
        tk.Label(title_frame, text="定 时 关 机",
                 font=("Microsoft YaHei", 20, "bold"),
                 bg=self.BG, fg=self.TITLE).pack(side="left")
        tk.Label(title_frame, text="🎀", font=("Segoe UI Symbol", 18),
                 bg=self.BG).pack(side="left", padx=(4, 0))

        # 副标题
        tk.Label(self.root, text="♡ 设置时间后自动关闭计算机 ♡",
                 font=("Microsoft YaHei", 9),
                 bg=self.BG, fg=self.SUB).pack(pady=(2, 6))

        # ── 小星星分隔 ──
        tk.Label(self.root,
                 text="✧  ⋅ ✧  ⋅ ✧  ⋅ ✧  ⋅ ✧  ⋅ ✧  ⋅ ✧",
                 font=("Segoe UI Symbol", 7),
                 bg=self.BG, fg=self.LACE_DARK).pack(pady=(0, 8))

        # ── 预设按钮 ──
        preset_frame = tk.Frame(self.root, bg=self.BG)
        preset_frame.pack(pady=(0, 14))

        presets = [
            ("🌸 15 分钟", 0, 15), ("🩷 30 分钟", 0, 30),
            ("🎀 1 小时", 1, 0), ("💖 2 小时", 2, 0), ("👑 3 小时", 3, 0)
        ]

        for text, h, m in presets:
            btn = tk.Label(
                preset_frame, text=text,
                font=("Microsoft YaHei", 9),
                bg=self.INPUT_BG, fg=self.TITLE,
                relief="solid", bd=1,
                padx=10, pady=4,
                cursor="hand2"
            )
            btn.bind("<Button-1>", lambda e, h=h, m=m: self.set_preset(h, m))
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=self.LACE, relief="solid"))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg=self.INPUT_BG, relief="solid"))
            btn.pack(side="left", padx=3)

        # ── 时间输入 ──
        time_frame = tk.Frame(self.root, bg=self.BG)
        time_frame.pack(pady=(0, 2))

        tk.Label(time_frame, text="时", font=("Microsoft YaHei", 11),
                 bg=self.BG, fg=self.TITLE).pack(side="left", padx=(0, 8))

        self.hours_var = tk.StringVar(value="0")
        self.hours_entry = tk.Entry(
            time_frame, textvariable=self.hours_var,
            font=("Microsoft YaHei", 22, "bold"),
            bg=self.INPUT_BG, fg=self.TITLE,
            insertbackground=self.ACCENT,
            relief="solid", bd=1, width=3, justify="center",
            highlightbackground=self.INPUT_BORDER,
            highlightcolor=self.ACCENT,
            highlightthickness=2
        )
        self.hours_entry.pack(side="left")

        tk.Label(time_frame, text="  ♡  ", font=("Microsoft YaHei", 14),
                 bg=self.BG, fg=self.LACE_DARK).pack(side="left")

        tk.Label(time_frame, text="分", font=("Microsoft YaHei", 11),
                 bg=self.BG, fg=self.TITLE).pack(side="left", padx=(0, 8))

        self.minutes_var = tk.StringVar(value="30")
        self.minutes_entry = tk.Entry(
            time_frame, textvariable=self.minutes_var,
            font=("Microsoft YaHei", 22, "bold"),
            bg=self.INPUT_BG, fg=self.TITLE,
            insertbackground=self.ACCENT,
            relief="solid", bd=1, width=3, justify="center",
            highlightbackground=self.INPUT_BORDER,
            highlightcolor=self.ACCENT,
            highlightthickness=2
        )
        self.minutes_entry.pack(side="left")

        # 提示
        tk.Label(self.root, text="输入 0 时 30 分 = 30 分钟后关机",
                 font=("Microsoft YaHei", 8), bg=self.BG, fg=self.SUB
                 ).pack(pady=(4, 10))

        # ── 使用中保护开关 ──
        self.protect_var = tk.BooleanVar(value=True)
        self.protect_cb = tk.Checkbutton(
            self.root,
            text="🛡️ 使用中保护：正在用电脑就不关机，空闲才关",
            variable=self.protect_var,
            font=("Microsoft YaHei", 9),
            bg=self.BG, fg=self.TITLE,
            activebackground=self.BG, activeforeground=self.TITLE,
            selectcolor=self.INPUT_BG,
            highlightthickness=0, bd=0, cursor="hand2"
        )
        self.protect_cb.pack(pady=(0, 12))

        # ── 按钮 ──
        btn_frame = tk.Frame(self.root, bg=self.BG)
        btn_frame.pack(pady=(0, 10))

        self.start_btn = tk.Label(
            btn_frame,
            text="💕  开 始 计 时  💕",
            font=("Microsoft YaHei", 12, "bold"),
            bg=self.ACCENT, fg=self.TEXT_ON_PINK,
            relief="flat", bd=0,
            padx=24, pady=12,
            cursor="hand2"
        )
        self.start_btn.bind("<Button-1>", lambda e: self.start_shutdown())
        self.start_btn.bind("<Enter>", lambda e: self.start_btn.config(bg=self.ACCENT2))
        self.start_btn.bind("<Leave>", lambda e: self.start_btn.config(
            bg=self.LACE_DARK if self.timer_running else self.ACCENT))
        self.start_btn.pack(side="left", padx=4)

        self.cancel_btn = tk.Label(
            btn_frame,
            text="✕  取 消 关 机",
            font=("Microsoft YaHei", 12, "bold"),
            bg=self.CANCEL_BG, fg=self.TEXT_ON_PINK,
            relief="flat", bd=0,
            padx=24, pady=12,
            cursor="hand2"
        )
        self.cancel_btn.bind("<Button-1>", lambda e: self.cancel_shutdown())
        self.cancel_btn.bind("<Enter>", lambda e: self.cancel_btn.config(bg=self.CANCEL_HOVER))
        self.cancel_btn.bind("<Leave>", lambda e: self.cancel_btn.config(bg=self.CANCEL_BG))
        self.cancel_btn.pack(side="left", padx=4)

        # ── 状态 ──
        self.status_var = tk.StringVar(value="等待设置...")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var,
            font=("Microsoft YaHei", 11, "bold"),
            bg=self.BG, fg=self.STATUS_IDLE
        )
        self.status_label.pack(pady=(8, 0))

        # ── 底部蕾丝 ──
        bottom_lace = tk.Canvas(self.root, width=420, height=56,
                                bg=self.BG, highlightthickness=0, bd=0)
        bottom_lace.pack(side="bottom")
        self.draw_lace_bottom(bottom_lace)

    # ═══════════════════════════════════════
    #  逻辑
    # ═══════════════════════════════════════

    def set_preset(self, h, m):
        self.hours_var.set(str(h))
        self.minutes_var.set(str(m))

    def get_total_seconds(self):
        try:
            h = int(self.hours_var.get())
        except ValueError:
            h = 0
        try:
            m = int(self.minutes_var.get())
        except ValueError:
            m = 0
        if h < 0:
            h = 0
        if m < 0:
            m = 0
        return h * 3600 + m * 60

    def start_shutdown(self):
        total = self.get_total_seconds()
        if total <= 0:
            self.status_var.set("💢 请设置大于 0 的时间")
            self.status_label.config(fg=self.STATUS_ALERT)
            return
        if total > 86400:
            self.status_var.set("💢 最长支持 24 小时")
            self.status_label.config(fg=self.STATUS_ALERT)
            return

        shutdown_cmd("/a")
        if self.protect_var.get():
            # 保险模式: OS 定时多留 5 分钟作兜底, 真正的决定在计时结束时做
            shutdown_cmd("/s", "/t", str(total + FAILSAFE_PAD))
        else:
            shutdown_cmd("/s", "/t", str(total))

        self.total_seconds = total
        self.deadline = self._now() + total
        self.timer_running = True
        self.final_grace = False
        self.in_postpone = False
        self.remaining_seconds = total
        self.save_state()
        self.update_ui()
        self.update_countdown()
        self.after_id = self.root.after(1000, self.tick)

    def cancel_shutdown(self):
        shutdown_cmd("/a")
        self.clear_state()
        self.timer_running = False
        self.final_grace = False
        self.in_postpone = False
        self.deadline = None
        self.remaining_seconds = 0
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.update_ui()
        self.status_var.set("💤 已取消定时关机")
        self.status_label.config(fg=self.STATUS_IDLE)

    def remaining_from_deadline(self):
        """按截止时刻反算剩余秒数 —— 不用每秒自减, 所以不会累积漂移"""
        if self.deadline is None:
            return 0
        return max(0, int(math.ceil(self.deadline - self._now())))

    def tick(self):
        if not self.timer_running:
            return
        self.remaining_seconds = self.remaining_from_deadline()
        if self.remaining_seconds <= 0:
            if self.protect_var.get() and not self.final_grace:
                # 保险模式: 到点先撤旧定时, 看用户是否在用
                self.check_user_activity()
                if not self.timer_running:
                    return
            else:
                self.timer_running = False
                self.deadline = None
                self.clear_state()
                self.update_ui()
                self.status_var.set("🌙 正在关机，晚安...")
                self.status_label.config(fg=self.STATUS_ALERT)
                return
        else:
            self.update_countdown()
        self.after_id = self.root.after(1000, self.tick)

    def check_user_activity(self):
        """保险机制: 计时结束时的最终裁决——空闲才关机, 使用中顺延"""
        shutdown_cmd("/a")
        idle = self.get_idle_seconds()
        if idle >= IDLE_THRESHOLD:
            # 空闲 → 关机(留 60 秒反悔窗口)
            shutdown_cmd("/s", "/t", str(FINAL_GRACE))
            self.final_grace = True
            self.in_postpone = False
            self.deadline = self._now() + FINAL_GRACE
            self.remaining_seconds = FINAL_GRACE
            self.status_var.set("🌙 空闲检测通过，60 秒后关机（可点取消反悔）")
            self.status_label.config(fg=self.STATUS_ALERT)
        else:
            # 使用中 → 顺延, 并重新布 OS 兜底定时
            shutdown_cmd("/s", "/t", str(POSTPONE_SECONDS + FAILSAFE_PAD))
            self.final_grace = False
            self.in_postpone = True
            self.deadline = self._now() + POSTPONE_SECONDS
            self.remaining_seconds = POSTPONE_SECONDS
            self.status_var.set(f"♡ 检测到使用中，{POSTPONE_SECONDS // 60} 分钟后再检查")
            self.status_label.config(fg=self.STATUS_WARN)
        self.save_state()

    @staticmethod
    def get_idle_seconds():
        """距上次键盘/鼠标输入多少秒; -1 = API 失败(按'使用中'处理, 保险优先)"""
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return ((tick - lii.dwTime) & 0xFFFFFFFF) // 1000
        return -1

    def update_countdown(self):
        h = self.remaining_seconds // 3600
        m = (self.remaining_seconds % 3600) // 60
        s = self.remaining_seconds % 60
        if self.final_grace:
            self.status_var.set(f"🌙 空闲通过，{m:02d}:{s:02d} 后关机（可点取消反悔）")
            self.status_label.config(fg=self.STATUS_ALERT)
        elif self.in_postpone:
            self.status_var.set(f"♡ 使用中，{m:02d}:{s:02d} 后再次检查")
            self.status_label.config(fg=self.STATUS_WARN)
        else:
            self.status_var.set(f"⏳ {h:02d}:{m:02d}:{s:02d} 后关机")
            self.status_label.config(fg=self.STATUS_ACTIVE)

    def update_ui(self):
        self.protect_cb.config(state="disabled" if self.timer_running else "normal")
        if self.timer_running:
            self.hours_entry.config(state="disabled", disabledbackground=self.INPUT_BG,
                                     disabledforeground=self.LACE_DARK)
            self.minutes_entry.config(state="disabled", disabledbackground=self.INPUT_BG,
                                       disabledforeground=self.LACE_DARK)
            self.start_btn.config(bg=self.LACE_DARK, fg=self.TEXT_ON_PINK)
        else:
            self.hours_entry.config(state="normal")
            self.minutes_entry.config(state="normal")
            self.start_btn.config(bg=self.ACCENT, fg=self.TEXT_ON_PINK)

    def on_close(self):
        """关窗 = 结束程序(省资源); OS 定时仍在跑, 下次打开能认出并取消"""
        self.root.destroy()


if __name__ == "__main__":
    ShutdownTimer()
