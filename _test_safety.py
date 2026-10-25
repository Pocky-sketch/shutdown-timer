# -*- coding: utf-8 -*-
"""定时关机 - 集成测试

规则: 被测逻辑一律跑真实代码路径(真 tkinter 窗口/真事件/真状态文件),
      只有"真会关机"这一条边界上打桩 subprocess(Phase B 另有真实端到端)。

分两阶段:
  Phase A  打桩 shutdown 调用, 验证全部计数/顺延/宽限/漂移/恢复逻辑
  Phase B  真实调用 shutdown.exe, 验证"真的布上定时 / 真的撤掉"(安全: 1 小时定时, 立刻撤)

运行: python _test_safety.py
"""
import sys
import os
import io
import json
import time
import ctypes
import tkinter as tk
import subprocess
import importlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

tk.Tk.mainloop = lambda self=None: None        # 不让主循环阻塞

import 定时关机源码 as m                        # noqa: E402

ORIG_GET_IDLE = m.ShutdownTimer.get_idle_seconds   # 打桩前的原始 API 实现

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


# ══════════════════════════════════════════════════════════
#  打桩层: 记录所有 shutdown 调用(含 creationflags), 不真关机
# ══════════════════════════════════════════════════════════
REAL_RUN = subprocess.run
calls = []            # [(argv, kwargs), ...]


def fake_run(cmd, **kw):
    calls.append((list(cmd), kw))
    return subprocess.CompletedProcess(cmd, 0)


subprocess.run = fake_run


def argv():
    """实时取命令行列表(每次调用都重新算, 不要用快照)"""
    return [c[0] for c in calls]


# ══════════════════════════════════════════════════════════
#  工具: 假时钟(计时改成"按截止时刻反算"后, 需要能精确控制时间)
# ══════════════════════════════════════════════════════════
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def new_app(**kw):
    kw.setdefault("single_instance", False)
    kw.setdefault("restore_state", False)
    app = m.ShutdownTimer(**kw)
    app.root.withdraw()
    return app


def ticks(app, n, clock=None, dt=1.0):
    for _ in range(n):
        if clock:
            clock.advance(dt)
        app.tick()


# 状态文件写到临时目录, 不污染 %LOCALAPPDATA%
STATE_DIR = os.path.join(os.environ.get("TEMP", "."), "shutdown_timer_test")
os.makedirs(STATE_DIR, exist_ok=True)
TEST_STATE = os.path.join(STATE_DIR, "state.json")
m.state_path = lambda: TEST_STATE
if os.path.exists(TEST_STATE):
    os.remove(TEST_STATE)

print("=== Phase A: 打桩验证逻辑 ===")
clock = Clock()
app = new_app()
app._now = clock
app.root.withdraw()

# ── 场景1: 保护开 + 空闲 → 关机(60s 宽限) ──
calls.clear()
app.protect_var.set(True)
app.hours_var.set("0"); app.minutes_var.set("1")
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 99999)  # 空闲
app.start_shutdown()
check("S1 启动: 保护模式布 total+300 兜底",
      argv() == [["shutdown", "/a"], ["shutdown", "/s", "/t", "360"]], str(argv()))
check("S1 初始状态", app.timer_running and not app.final_grace
      and not app.in_postpone and app.remaining_seconds == 60)
ticks(app, 60, clock)
check("S1 到点: 撤旧定时+发60s关机",
      argv()[-2:] == [["shutdown", "/a"], ["shutdown", "/s", "/t", "60"]], str(argv()[-2:]))
check("S1 final_grace 状态", app.final_grace and app.remaining_seconds == 60
      and app.timer_running, app.status_var.get())
ticks(app, 60, clock)
check("S1 宽限结束: 完成", (not app.timer_running) and "正在关机" in app.status_var.get(),
      app.status_var.get())

# ── 场景2: 保护开 + 使用中 → 顺延循环 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("2")
app.protect_var.set(True)
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 10)  # 正在用
app.start_shutdown()
check("S2 启动: 布 120+300=420 兜底", argv()[-1] == ["shutdown", "/s", "/t", "420"], str(argv()[-1]))
ticks(app, 120, clock)
check("S2 到点: 撤定时+顺延布600",
      argv()[-2:] == [["shutdown", "/a"], ["shutdown", "/s", "/t", "600"]], str(argv()[-2:]))
check("S2 顺延状态", app.in_postpone and app.remaining_seconds == 300
      and app.timer_running and "使用中" in app.status_var.get(), app.status_var.get())
ticks(app, 1, clock)
check("S2 顺延倒计时文案", "使用中" in app.status_var.get() and "再次检查" in app.status_var.get(),
      app.status_var.get())

# ── 场景3: 先使用中顺延, 后空闲 → 最终关机 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("1")
app.protect_var.set(True)
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 0)
app.start_shutdown()
ticks(app, 60, clock)
check("S3 第一次到点: 顺延", app.in_postpone and "使用中" in app.status_var.get(),
      app.status_var.get())
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 99999)
ticks(app, 300, clock)
check("S3 第二次到点: 进入关机宽限", app.final_grace and "空闲" in app.status_var.get(),
      app.status_var.get())
ticks(app, 60, clock)
check("S3 完成", (not app.timer_running) and "正在关机" in app.status_var.get(),
      app.status_var.get())

# ── 场景4: 保护关 = 旧行为 ──
calls.clear()
app.protect_var.set(False)
app.hours_var.set("0"); app.minutes_var.set("1")
app.start_shutdown()
check("S4 启动: 直接布 60, 无兜底", argv()[-1] == ["shutdown", "/s", "/t", "60"], str(argv()[-1]))
ticks(app, 60, clock)
check("S4 到点: 直接完成, 不再查", (not app.timer_running)
      and "正在关机" in app.status_var.get(), app.status_var.get())

# ── 场景5: 取消复位 ──
calls.clear()
app.hours_var.set("1"); app.minutes_var.set("0")
app.protect_var.set(True)
app.start_shutdown()
ticks(app, 5, clock)
app.cancel_shutdown()
check("S5 取消: 全部复位", (not app.timer_running) and not app.final_grace
      and not app.in_postpone and "取消" in app.status_var.get(), app.status_var.get())

# ── 场景6: 真实 GetLastInputInfo API ──
idle = ORIG_GET_IDLE()                       # 用打桩前的原始实现, 真调 Win32
check("S6 真实API: 0~600s 范围", 0 <= idle <= 600, f"idle={idle}s")

# ── 场景7: 输入校验回归 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("0")
app.start_shutdown()
check("S7 0秒被拒", (not app.timer_running) and "大于 0" in app.status_var.get(),
      app.status_var.get())
app.hours_var.set("25"); app.minutes_var.set("0")
app.start_shutdown()
check("S7 超24h被拒", (not app.timer_running) and "24 小时" in app.status_var.get(),
      app.status_var.get())

# ── 场景8(新): 修漂移 —— 计时按截止时刻反算, tick 迟到也不错 ──
calls.clear()
app.hours_var.set("1"); app.minutes_var.set("0")
app.protect_var.set(True)
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 99999)
app.start_shutdown()
clock.advance(3599.7)                 # 极端情况: 距开始 3599.7 秒才 tick 一次
ticks(app, 1, clock, dt=0.0)
check("S8 按时刻反算: 只剩 1 秒, 仍在计时",
      app.timer_running and app.remaining_seconds == 1,
      f"remaining={app.remaining_seconds}")
clock.advance(1.8)                    # 再迟到 1.5 秒 → 已过点
ticks(app, 1, clock, dt=0.0)
check("S8 过点: 直接进入关机判定(不再靠 tick 次数累加)",
      app.final_grace and argv()[-1] == ["shutdown", "/s", "/t", "60"], str(argv()[-1:]))
app.cancel_shutdown()

# ── 场景9(新): 关窗不取消定时(设计如此) ──
calls.clear()
app.hours_var.set("1"); app.minutes_var.set("0")
app.protect_var.set(True)
app.start_shutdown()
before = len(calls)
app.on_close()                        # 关窗 = 结束程序
check("S9 关窗: 不再发 shutdown /a (定时仍在)", len(calls) == before, str(argv()[-1:]))
check("S9 关窗: 状态文件留下(供重开识别)", os.path.exists(TEST_STATE), TEST_STATE)

# ── 场景10(新): 重开能认出上次的定时 ──
saved = json.load(open(TEST_STATE, encoding="utf-8"))
saved["deadline"] = time.time() + 1200          # 还剩 20 分钟
json.dump(saved, open(TEST_STATE, "w", encoding="utf-8"))
app2 = new_app(restore_state=True)
check("S10 重开恢复: 计时中 + 剩余时间正确",
      app2.timer_running and 1195 <= app2.remaining_seconds <= 1200,
      f"remaining={app2.remaining_seconds}")
check("S10 重开恢复: 文案提示可取消", "仍在进行中" in app2.status_var.get(),
      app2.status_var.get())
calls.clear()
app2.cancel_shutdown()
check("S10 重开取消: 真撤定时 + 清状态文件",
      argv()[-1] == ["shutdown", "/a"] and not os.path.exists(TEST_STATE), str(argv()[-1:]))
app2.root.destroy()

# ── 场景11(新): 跨开机/过期的状态文件作废 ──
json.dump({"boot_epoch": 0, "deadline": time.time() + 9999, "protect": True,
           "total": 9999, "phase": "counting"},
          open(TEST_STATE, "w", encoding="utf-8"))
app3 = new_app(restore_state=True)
check("S11 跨开机: 不恢复, 且清掉陈旧状态",
      (not app3.timer_running) and not os.path.exists(TEST_STATE), app3.status_var.get())
app3.root.destroy()

# ── 场景12(新): 单实例互斥 ──
app4 = new_app(single_instance=True)
second = m.ShutdownTimer.__new__(m.ShutdownTimer)      # 不建窗口, 直接测互斥体
got = second.acquire_single_instance()
check("S12 已有实例时拿不到互斥体(= 阻止第二开)", got is False, f"got={got}")
app4.root.destroy()
# 释放互斥体(两个句柄都要关), 否则后面的 S15 子进程会被当成"第二个实例"直接退出
for h in (app4._mutex, second._mutex):
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
app4._mutex = second._mutex = None

# ── 场景13(新): hover 颜色回归(真事件) ──
app5 = new_app()
app5.cancel_btn.event_generate("<Enter>")
app5.root.update()
hovered = app5.cancel_btn.cget("bg")
app5.cancel_btn.event_generate("<Leave>")
app5.root.update()
left = app5.cancel_btn.cget("bg")
check("S13 取消按钮: 悬停离开后回原色(不再永久亮红)",
      left == app5.CANCEL_BG and hovered == app5.CANCEL_HOVER,
      f"hover={hovered} leave={left}")
app5.start_btn.event_generate("<Enter>"); app5.root.update()
app5.start_btn.event_generate("<Leave>"); app5.root.update()
check("S13 开始按钮: 未计时时悬停离开回主粉",
      app5.start_btn.cget("bg") == app5.ACCENT, app5.start_btn.cget("bg"))
app5.root.destroy()

# ── 场景14(新): 所有 shutdown 调用都带 CREATE_NO_WINDOW ──
calls.clear()
app6 = new_app()
app6._now = Clock()
app6.protect_var.set(False)
app6.hours_var.set("0"); app6.minutes_var.set("10")
app6.start_shutdown()
app6.cancel_shutdown()
flags = [kw.get("creationflags") for _, kw in calls]
check("S14 全部 shutdown 调用带 CREATE_NO_WINDOW(不弹终端窗)",
      flags and all(f == m.CREATE_NO_WINDOW for f in flags), str(flags))
app6.root.destroy()

# ══════════════════════════════════════════════════════════
#  场景15(新): 删掉图标文件后程序仍能启动(真实子进程, 不看窗口只进程存活)
# ══════════════════════════════════════════════════════════
print("=== 场景15: 图标缺失仍能启动 (真实子进程) ===")
import shutil
import tempfile

PYW = sys.executable.replace("python.exe", "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable


def launch_alive(with_icon):
    tmp = tempfile.mkdtemp(prefix="shutdown_timer_")
    shutil.copy(os.path.join(HERE, "定时关机源码.py"), tmp)
    if with_icon:
        shutil.copy(os.path.join(HERE, "定时关机图标.ico"), tmp)
    p = subprocess.Popen([PYW, os.path.join(tmp, "定时关机源码.py")],
                         cwd=tmp, creationflags=m.CREATE_NO_WINDOW)
    time.sleep(4)
    alive = p.poll() is None
    if alive:
        p.kill(); p.wait()
    shutil.rmtree(tmp, ignore_errors=True)
    return alive


check("S15 有图标: 正常常驻(窗口进程存活)", launch_alive(True))
check("S15 无图标: 仍能启动(不再 0 秒退出)", launch_alive(False), )

# ══════════════════════════════════════════════════════════
#  Phase B: 真实 shutdown.exe 端到端(布 1 小时定时 → 立刻撤, 全程秒级)
# ══════════════════════════════════════════════════════════
print("=== Phase B: 真实端到端 (1 小时定时, 立刻撤) ===")
subprocess.run = REAL_RUN

base = REAL_RUN(["shutdown", "/a"], capture_output=True,
                creationflags=m.CREATE_NO_WINDOW).returncode
check("B1 前置: 当前没有待执行的关机(错误码 1116)", base == 1116, f"rc={base}")

app7 = new_app()
app7.protect_var.set(True)
app7.hours_var.set("1"); app7.minutes_var.set("0")
app7.start_shutdown()                       # 真实布: shutdown /s /t 3900
armed = REAL_RUN(["shutdown", "/a"], capture_output=True,
                 creationflags=m.CREATE_NO_WINDOW).returncode
check("B2 开始计时: 系统里真的挂上了关机定时(撤掉返回 0)", armed == 0, f"rc={armed}")

app7.cancel_shutdown()                      # 真实撤
after = REAL_RUN(["shutdown", "/a"], capture_output=True,
                 creationflags=m.CREATE_NO_WINDOW).returncode
check("B3 取消关机: 系统定时真的没了(错误码 1116)", after == 1116, f"rc={after}")
app7.root.destroy()
if os.path.exists(TEST_STATE):
    os.remove(TEST_STATE)

# ── 汇总 ──
fails = [r for r in results if not r[1]]
print("---")
print("TOTAL:", len(results), " FAIL:", len(fails))
for n, _ in fails:
    print("  FAILED:", n)
sys.exit(1 if fails else 0)
