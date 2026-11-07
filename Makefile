# 股票论坛可视化查看器 —— 常用命令（统一走 .venv，避免默认 python 缺依赖）
# 用法: make run | make test | make freeze

PY := .venv/Scripts/python.exe
ifeq ($(OS),Windows_NT)
	PY := .venv/Scripts/python.exe
else
	PY := .venv/bin/python
endif

.PHONY: run test freeze install

# 自举：干净机器上 .venv 不存在时，install/run/test 先建虚拟环境再执行，
# 避免「重装即炸」（P2：依赖固化）。.venv 已存在则 make 按时间戳跳过，不会重建。
.venv:
	python -m venv .venv
	.venv/Scripts/python.exe -m pip install --upgrade pip

install: .venv
	$(PY) -m pip install -r requirements.lock.txt

run: .venv
	$(PY) viewer/server.py

test: .venv
	$(PY) -m pytest -q

freeze: .venv
	$(PY) -m pip freeze > requirements.lock.txt
