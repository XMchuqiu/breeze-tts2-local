"""Breeze Voice Studio 启动器（可打包为独立 exe）。

模块划分：
- core.py  路径定位、解释器探测、配置、子进程托管（无 GUI 依赖）
- ui.py    tkinter 界面
- tray.py  系统托盘（可选）
- main.py  入口与命令行自检
"""

__version__ = "1.0.0"
