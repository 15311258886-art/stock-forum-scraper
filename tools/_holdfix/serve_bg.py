# -*- coding: utf-8 -*-
"""Throwaway viewer server for headless UI probes (new code, isolated port).

Run:  .venv\\Scripts\\python.exe tools\\_holdfix\\serve_bg.py   (HOLDFIX_PORT env overrides 8792)
Serves viewer/static with the CURRENT on-disk code — the live 8765 instance is untouched.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

import server as S  # noqa: E402

PORT = int(os.environ.get("HOLDFIX_PORT", "8792"))
srv = S.create_server(port=PORT)
print("[holdfix] serving on %d" % PORT, flush=True)
srv.serve_forever()
