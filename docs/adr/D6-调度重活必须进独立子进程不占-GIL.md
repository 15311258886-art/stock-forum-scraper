## D6 调度重活必须进独立子进程（不占 GIL）
- **背景**：查看器单进程多线程，纯 Python 长任务占 GIL 导致全站卡顿（实测 pillars 单次 36.7s 冻全站）。
- **结论**：所有纯计算型重活走 `tools/sched_job.py` 子进程（`_spawn_offproc`），I/O 型（网络 / 文件）不必。
- **影响模块**：`server._sched_tasks`、`tools/sched_job.py`。
