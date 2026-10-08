# -*- coding: utf-8 -*-
"""阶段 0 测试自检闸（Mutation / Fault-Injection Gate）。

读 stage0_mutations.json，对每个 expect_fail=true 的种子变异执行：
  1) 备份 target（.bak）并记录 sha256；
  2) 用 anchor 在文件内 find/replace 成 inject（找不到 anchor -> 报错 exit 1，绝不静默跳过）；
  3) subprocess 运行对应正确性测试（python -m pytest 优先，否则回退 python -c）；
  4) finally 强制还原文件并比对 sha256 确认无损；
  5) 判定：施加变异后该用例仍 PASS（exit 0）-> 打印 MUTATION <id> NOT CAUGHT 并累计失败；
     全部被捕获则打印 ALL MUTATIONS CAUGHT。任意变异未被捕获 -> 打印 BUILD FAIL 并 exit 1；全捕获 -> exit 0。

legacy demo（expect_fail=false）：用该变异跑现有 verify_backend.py，若仍 PASS ->
打印 LEGACY SUITE BLIND: verify_backend.py passed despite injected defect（仅演示，不计 BUILD FAIL）。

提供 --dry-run：只打印计划，不改动、不跑。

红线：变异只在「备份副本 / 原地备份 + 强制还原」下施加，sha256 校验保证仓库零污染。
"""
import sys
import os
import json
import hashlib
import subprocess
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "stage0_mutations.json")
TESTFILE = os.path.join(ROOT, "test_stage0_correctness.py")
PY = sys.executable


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_correctness_test(test_fn):
    """subprocess 运行单个正确性用例。返回 (exit_code, combined_output)。"""
    probe = subprocess.run([PY, "-B", "-m", "pytest", "--version"],
                           capture_output=True, text=True, cwd=ROOT)
    if probe.returncode == 0:
        cmd = [PY, "-B", "-m", "pytest", "%s::%s" % (TESTFILE, test_fn), "-q"]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        return r.returncode, (r.stdout + r.stderr)
    # 回退：python -c 导入测试模块并调用函数（抛 AssertionError 或返回 False -> 非零退出）
    cmd = [PY, "-B", "-c",
           "import test_stage0_correctness as m, sys; sys.exit(0 if m.%s() else 1)" % test_fn]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr)


def run_verify_backend():
    r = subprocess.run([PY, "-B", "verify_backend.py"],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr)


def apply_mutation_with(content, anchor, inject, target):
    if anchor not in content:
        raise RuntimeError("anchor 未找到于 %s：\n%s" % (target, anchor))
    n = content.count(anchor)
    if n != 1:
        raise RuntimeError("anchor 在 %s 中出现 %d 次（必须唯一）：\n%s" % (target, n, anchor))
    return content.replace(anchor, inject, 1)


def process_seed_v2(m, not_caught):
    tid = m["id"]
    target_rel = m["target_file"]
    anchor = m["anchor"]
    inject = m["inject"]
    test_fn = m["test"]
    target = os.path.join(ROOT, target_rel)
    bak = target + ".stage0.bak"
    print("\n--- 种子 %s : %s ---" % (tid, target_rel))
    before = sha256_of(target)
    with open(target, "rb") as f:
        orig = f.read()
    with open(bak, "wb") as f:
        f.write(orig)
    try:
        content = apply_mutation_with(orig.decode("utf-8"), anchor, inject, target)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        print("  已注入变异（anchor 命中且唯一）")
        rc, out = run_correctness_test(test_fn)
        caught = (rc != 0)
        if caught:
            print("  MUTATION %s CAUGHT（用例 FAIL，exit=%d）" % (tid, rc))
        else:
            print("  MUTATION %s NOT CAUGHT（用例仍 PASS，exit=%d）" % (tid, rc))
            not_caught.append(tid)
        if rc != 0:
            tail = "\n".join(out.strip().splitlines()[-12:])
            print("  --- 失败用例输出（尾部）---\n" + tail)
    except Exception as e:  # noqa: BLE001
        print("  ERROR: %s" % e)
        not_caught.append(tid + "(error)")
    finally:
        with open(target, "wb") as f:
            f.write(orig)
        after = sha256_of(target)
        if after != before:
            print("  !! 还原校验失败：sha256 不匹配，请人工检查 %s" % target)
        else:
            print("  已还原并校验一致（sha256 匹配）")
        try:
            os.remove(bak)
        except OSError:
            pass
    return not_caught


def process_legacy(m):
    tid = m["id"]
    target = os.path.join(ROOT, m["target_file"])
    anchor = m["anchor"]
    inject = m["inject"]
    print("\n--- legacy demo %s : %s ---" % (tid, m["target_file"]))
    before = sha256_of(target)
    with open(target, "rb") as f:
        orig = f.read()
    with open(target + ".stage0.bak", "wb") as f:
        f.write(orig)
    try:
        content = apply_mutation_with(orig.decode("utf-8"), anchor, inject, target)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        rc, out = run_verify_backend()
        if rc == 0:
            print("  LEGACY SUITE BLIND: verify_backend.py passed despite injected defect (exit=0)")
        else:
            print("  LEGACY SUITE NOT BLIND: verify_backend.py failed (exit=%d) -- 旧套件居然抓到了？" % rc)
    except Exception as e:  # noqa: BLE001
        print("  ERROR: %s" % e)
    finally:
        with open(target, "wb") as f:
            f.write(orig)
        after = sha256_of(target)
        if after != before:
            print("  !! 还原校验失败")
        try:
            os.remove(target + ".stage0.bak")
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="阶段0 测试自检闸")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改动、不跑")
    args = ap.parse_args()

    with open(MANIFEST, "r", encoding="utf-8") as f:
        mutations = json.load(f)

    seeds = [m for m in mutations if m.get("expect_fail") is True]
    legacy = [m for m in mutations if m.get("expect_fail") is not True]

    print("=== 阶段0 测试自检闸 ===")
    print("变异总数=%d（种子=%d, legacy=%d）" % (len(mutations), len(seeds), len(legacy)))

    if args.dry_run:
        for m in mutations:
            print("[dry-run] %s -> %s  (test=%s, expect_fail=%s)" % (
                m["id"], m["target_file"], m["test"], m.get("expect_fail")))
        return 0

    not_caught = []
    for m in seeds:
        process_seed_v2(m, not_caught)

    for m in legacy:
        process_legacy(m)

    print("\n=== 自检闸结论 ===")
    if not_caught:
        print("BUILD FAIL: 以下种子未被捕获（变异后用例仍 PASS）：%s" % ", ".join(not_caught))
        return 1
    print("ALL MUTATIONS CAUGHT: 5 个种子缺陷全部被正确性断言捕获（变异后用例 FAIL），构建通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
