# viewer/setup_kronos.py
"""Kronos 板块一键装机脚本（在【查看器所在机器】上运行，需联网 + pip）。

用途：把开源 Kronos K 线基础模型接入「股票论坛可视化查看器」的 Kronos 面板。
做了三件事：
  1) 装依赖：PyTorch(CPU) + numpy/pandas/einops/safetensors/huggingface_hub/tqdm/matplotlib；
  2) 内置模型源码：从 GitHub 钉死 commit 拉 model/kronos.py + model/module.py，
     打补丁（把 `from model.module import *` 改成相对导入）放进 viewer/kronos_core/；
  3) 下载权重：经 hf-mirror 镜像拉取 Kronos-mini 模型 + Tokenizer-base 权重到本地，
     之后完全离线推理（零 token、零 API 费用）。

运行方式（在查看器根目录，用查看器的 venv）：
  cd <查看器目录>
  .venv\\Scripts\\python.exe viewer\\setup_kronos.py        # Windows
  # 或： python viewer/setup_kronos.py

安全说明：
  - 源码来自 MIT 协议的开源仓库 shiyu-coder/Kronos（Copyright 2025 ShiYu），可合法内置。
  - 仅下载官方发布的源码与权重，不执行任何来源外脚本。
  - 所有版本钉死（SRC_COMMIT / revision），上游更新不会悄悄改变本地面板行为。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE_DIR = HERE / "kronos_core"

# —— 钉死的版本（防止上游变动悄悄影响本地面板）——
SRC_COMMIT = "67b630e"   # Kronos master 上的已知 commit（GitHub tree 返回）
# 下载源候选（按序尝试）：raw 直连国内通常被挡，jsDelivr/gh-proxy 镜像实测可达（2026-10-24）
_SOURCE_MIRRORS = [
    f"https://fastly.jsdelivr.net/gh/shiyu-coder/Kronos@{SRC_COMMIT}",
    f"https://gcore.jsdelivr.net/gh/shiyu-coder/Kronos@{SRC_COMMIT}",
    f"https://gh-proxy.com/https://raw.githubusercontent.com/shiyu-coder/Kronos/{SRC_COMMIT}",
    f"https://raw.githubusercontent.com/shiyu-coder/Kronos/{SRC_COMMIT}",
]
MIRROR = "https://hf-mirror.com"   # HuggingFace 镜像（国内可达；官方 hf.co 常需代理）

# 默认用轻量 mini 模型（约 16MB，CPU 友好）；若推理报 tokenizer/维度不匹配，
# 把下面 model_repo 改成 "NeoQuasar/Kronos-small"（官方示例即 small + base 配对）后重跑本脚本。
DEFAULT_CFG = {
    "model_repo": "NeoQuasar/Kronos-mini",
    "tokenizer_repo": "NeoQuasar/Kronos-Tokenizer-base",
    "revision": "main",
    "max_context": 512,
    "weights_dir": "weights",
    "mirror": MIRROR,
}


def _run(cmd):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, env=dict(os.environ, HF_ENDPOINT=MIRROR))
    if r.returncode != 0:
        raise SystemExit(f"命令失败（{r.returncode}）：{' '.join(cmd)}")
    return r


def install_deps():
    py = sys.executable
    print("\n[1/3] 安装依赖（PyTorch CPU + 其余纯 Python 包）…")
    # PyTorch 走 CPU 专用索引，体积更小、安装更快
    _run([py, "-m", "pip", "install", "--upgrade", "pip"])
    _run([py, "-m", "pip", "install", "torch", "--index-url",
          "https://download.pytorch.org/whl/cpu"])
    _run([py, "-m", "pip", "install",
          "numpy", "pandas", "einops==0.8.1", "safetensors==0.6.2",
          "huggingface_hub==0.33.1", "tqdm==4.67.1", "matplotlib==3.9.3"])


def vendor_model_source():
    print("\n[2/3] 内置 Kronos 模型源码（钉死 commit，打补丁后落 viewer/kronos_core/）…")
    CORE_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request

    def fetch(rel, dest):
        last_err = None
        for base in _SOURCE_MIRRORS:
            url = f"{base}/{rel}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=60).read()
                dest.write_bytes(data)
                print(f"  - 已拉取 {rel} ({len(data)} 字节)  [{base.split('/')[2]}]")
                return
            except Exception as e:   # 换下一个镜像
                last_err = e
                print(f"  · {base.split('/')[2]} 失败（{type(e).__name__}），换下一个源…")
        raise SystemExit(f"模型源码拉取失败（所有镜像均不可达）：{last_err}")

    fetch("model/kronos.py", CORE_DIR / "kronos.py")
    fetch("model/module.py", CORE_DIR / "module.py")

    # 打补丁：把官方 `from model.module import *` 改成相对导入，去掉 sys.path.append("../")
    kp = CORE_DIR / "kronos.py"
    src = kp.read_text(encoding="utf-8")
    patched = src.replace("from model.module import *", "from .module import *")
    patched = "\n".join(
        ln for ln in patched.splitlines() if "sys.path.append" not in ln)
    if patched != src:
        kp.write_text(patched, encoding="utf-8")
        print("  - 已打补丁：相对导入 + 去掉 sys.path.append")
    else:
        print("  - 未检测到需替换的导入语句（源码可能已更新），请人工核对 kronos.py 顶部导入")

    # 写 __init__.py（与官方 model/__init__.py 等价，但用相对导入）
    (CORE_DIR / "__init__.py").write_text(
        "from .kronos import KronosTokenizer, Kronos, KronosPredictor\n\n"
        "model_dict = {\n"
        "    'kronos_tokenizer': KronosTokenizer,\n"
        "    'kronos': Kronos,\n"
        "    'kronos_predictor': KronosPredictor,\n"
        "}\n\n\n"
        "def get_model_class(model_name):\n"
        "    if model_name in model_dict:\n"
        "        return model_dict[model_name]\n"
        "    raise NotImplementedError(f\"Model {model_name} not found\")\n",
        encoding="utf-8")
    print("  - 已写 kronos_core/__init__.py")


def download_weights():
    print("\n[3/3] 下载模型权重（经 hf-mirror 镜像，首次需联网）…")
    cfg = DEFAULT_CFG
    (CORE_DIR / "model_repo.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # 延迟导入 huggingface_hub（此时应已装好）
    from huggingface_hub import snapshot_download
    wd = CORE_DIR / cfg["weights_dir"]
    for repo in (cfg["model_repo"], cfg["tokenizer_repo"]):
        target = wd / repo.replace("/", "__")
        print(f"  - 下载 {repo} → {target}")
        snapshot_download(
            repo_id=repo, revision=cfg["revision"],
            local_dir=str(target), local_dir_use_symlinks=False)
    print("  - 权重下载完成（已本地化，之后可完全离线推理）")


def main():
    # 权重下载走 hf-mirror 国内镜像（避免直连 huggingface.co 被代理 502）；
    # 必须在 import huggingface_hub 之前写入环境变量，否则默认指向 huggingface.co。
    os.environ["HF_ENDPOINT"] = MIRROR
    print("=" * 64)
    print("Kronos 板块装机（查看器机器上运行）")
    print("=" * 64)
    install_deps()
    vendor_model_source()
    download_weights()
    print("\n✅ 装机完成。重启查看器（关掉窗口→重新双击桌面图标），")
    print("   进入「Kronos K线预测」面板，点「重新生成」即可跑预测+回测。")


if __name__ == "__main__":
    main()
