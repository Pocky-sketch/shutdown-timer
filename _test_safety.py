# -*- coding: utf-8 -*-
"""定时关机保险机制 - 集成测试(打桩 subprocess/get_idle_seconds, 不真关机)"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'F:\Tools\自制定时关机')

import tkinter as tk
import subprocess

tk.Tk.mainloop = lambda self=None: None  # 不让主循环阻塞

import 定时关机源码 as m

# 打桩 subprocess.run, 捕获所有 shutdown 命令
calls = []
def fake_run(cmd, **kw):
    calls.append(list(cmd))
    return None
subprocess.run = fake_run

orig_get_idle = m.ShutdownTimer.get_idle_seconds  # 原始静态函数(无参)

results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), name, detail)

app = m.ShutdownTimer()
app.root.withdraw()

# ── 场景1: 保护开 + 空闲 → 关机(60s 宽限) ──
calls.clear()
app.protect_var.set(True)
app.hours_var.set("0"); app.minutes_var.set("1")
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 99999)  # 空闲
app.start_shutdown()
check("S1 启动: 保护模式布 total+300 兜底", calls == [["shutdown", "/a"], ["shutdown", "/s", "/t", "360"]], str(calls))
check("S1 初始状态", app.timer_running and not app.final_grace and not app.in_postpone and app.remaining_seconds == 60)
for _ in range(60):
    app.tick()
check("S1 到点: 撤旧定时+发60s关机", calls[-2:] == [["shutdown", "/a"], ["shutdown", "/s", "/t", "60"]], str(calls[-2:]))
check("S1 final_grace 状态", app.final_grace and app.remaining_seconds == 60 and app.timer_running, app.status_var.get())
for _ in range(60):
    app.tick()
check("S1 宽限结束: 完成", (not app.timer_running) and "正在关机" in app.status_var.get(), app.status_var.get())

# ── 场景2: 保护开 + 使用中 → 顺延循环 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("2")
app.protect_var.set(True)
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 10)  # 正在用
app.start_shutdown()
check("S2 启动: 布 120+300=420 兜底", calls[-1] == ["shutdown", "/s", "/t", "420"], str(calls[-1]))
for _ in range(120):
    app.tick()
check("S2 到点: 撤定时+顺延布600", calls[-2:] == [["shutdown", "/a"], ["shutdown", "/s", "/t", "600"]], str(calls[-2:]))
check("S2 顺延状态", app.in_postpone and app.remaining_seconds == 300 and app.timer_running and "使用中" in app.status_var.get(), app.status_var.get())
app.tick()
check("S2 顺延倒计时文案", "使用中" in app.status_var.get() and "再次检查" in app.status_var.get(), app.status_var.get())

# ── 场景3: 先使用中顺延, 后空闲 → 最终关机 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("1")
app.protect_var.set(True)
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 0)  # 使用中
app.start_shutdown()
for _ in range(60):
    app.tick()
check("S3 第一次到点: 顺延", app.in_postpone and "使用中" in app.status_var.get(), app.status_var.get())
m.ShutdownTimer.get_idle_seconds = staticmethod(lambda: 99999)  # 现在空闲了
for _ in range(300):
    app.tick()
check("S3 第二次到点: 进入关机宽限", app.final_grace and "空闲" in app.status_var.get(), app.status_var.get())
for _ in range(60):
    app.tick()
check("S3 完成", (not app.timer_running) and "正在关机" in app.status_var.get(), app.status_var.get())

# ── 场景4: 保护关 = 旧行为 ──
calls.clear()
app.protect_var.set(False)
app.hours_var.set("0"); app.minutes_var.set("1")
app.start_shutdown()
check("S4 启动: 直接布 60, 无兜底", calls[-1] == ["shutdown", "/s", "/t", "60"], str(calls[-1]))
for _ in range(60):
    app.tick()
check("S4 到点: 直接完成, 不再查", (not app.timer_running) and "正在关机" in app.status_var.get(), app.status_var.get())

# ── 场景5: 取消复位 ──
calls.clear()
app.hours_var.set("1"); app.minutes_var.set("0")
app.protect_var.set(True)
app.start_shutdown()
for _ in range(5):
    app.tick()
app.cancel_shutdown()
check("S5 取消: 全部复位", (not app.timer_running) and not app.final_grace and not app.in_postpone and "取消" in app.status_var.get(), app.status_var.get())

# ── 场景6: 真实 GetLastInputInfo API ──
idle = orig_get_idle()
check("S6 真实API: 0~600s 范围", 0 <= idle <= 600, f"idle={idle}s")

# ── 场景7: 输入校验回归 ──
calls.clear()
app.hours_var.set("0"); app.minutes_var.set("0")
app.start_shutdown()
check("S7 0秒被拒", (not app.timer_running) and "大于 0" in app.status_var.get(), app.status_var.get())
app.hours_var.set("25"); app.minutes_var.set("0")
app.start_shutdown()
check("S7 超24h被拒", (not app.timer_running) and "24 小时" in app.status_var.get(), app.status_var.get())

app.root.destroy()
fails = [r for r in results if not r[1]]
print("---")
print("TOTAL:", len(results), " FAIL:", len(fails))
sys.exit(1 if fails else 0)
