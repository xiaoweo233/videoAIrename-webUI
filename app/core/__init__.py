"""core —— 引擎层：五阶段流水线（方案 §6）。

红线 G3：本层 **禁止 import tkinter / fastapi 等 UI 框架**，
以保证桌面壳与 Web 壳共用同一份引擎。
"""
