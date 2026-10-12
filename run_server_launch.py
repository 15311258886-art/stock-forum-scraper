import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
os.execv(venv_py, [venv_py, os.path.join(ROOT, "viewer", "server.py")])
