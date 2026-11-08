#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Round2 一键验收脚本（P0 修复验收 + 全量门禁）。

用法（在任意目录）：
    python accept_round.py                # quick：ruff + pytest + 官方用例(引擎关) + 静态红线 + P0 回归测试
    python accept_round.py --full         # 追加：web tsc/lint/build + docker build + smoke(自动起 API)
    python accept_round.py --full --strict-git   # 追加：强制要求工作区干净且已推送

常用参数：
    --repo PATH       仓库根目录（默认自动向上查找 scholar-agent/api/pyproject.toml）
    --min-tests N     pytest 通过数下限（默认 109；修复 P0 后应 ≥116）
    --min-p0-tests N  P0 回归测试条数下限（默认 7）
    --skip-cases      跳过官方用例（节省 ~3~8 分钟）
    --skip-smoke      跳过 smoke
    --skip-docker     跳过 docker build
    --skip-web        跳过前端门禁
    --keep-data       保留临时 SCHOLAR_DATA_DIR（默认放 runs/<ts>/tmp_data）

输出：
    runs/<时间戳>/summary.md、summary.json 及全部步骤日志
退出码：0 全部 PASS（SKIP 不算失败）；1 存在 FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUNS_DIR = Path(__file__).resolve().parent / "runs"


@dataclass
class Result:
    name: str
    status: str  # PASS / FAIL / SKIP / WARN
    detail: str = ""
    log: str = ""
    seconds: float = 0.0


@dataclass
class Ctx:
    repo: Path
    api: Path
    web: Path
    py: str
    node: str | None
    outdir: Path
    results: list[Result] = field(default_factory=list)
    args: argparse.Namespace | None = None

    def add(self, r: Result) -> Result:
        self.results.append(r)
        icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "WARN": "WARN"}[r.status]
        print(f"[{icon}] {r.name}" + (f" · {r.detail}" if r.detail else ""))
        return r

    def log_path(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        return self.outdir / f"{safe}.log"


def run(ctx: Ctx, name: str, cmd: list[str], *, cwd: Path | None = None,
        env: dict | None = None, timeout: int = 1800,
        pass_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """执行命令并落日志；返回 (returncode, 合并输出)。"""
    full_env = os.environ.copy()
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(cwd or ctx.repo), env=full_env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or "") + ("\n[stderr]\n" + p.stderr if p.stderr else "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
        code = -9
    except FileNotFoundError as e:
        out = f"[COMMAND NOT FOUND] {e}"
        code = -2
    elapsed = time.time() - t0
    ctx.log_path(name).write_text(f"$ {' '.join(cmd)}\n(cwd={cwd or ctx.repo})\n\n{out}",
                                  encoding="utf-8")
    tail = "\n".join([ln for ln in out.strip().splitlines() if ln.strip()][-8:])
    print(f"    └ {elapsed:.1f}s rc={code}")
    if tail:
        for ln in tail.splitlines()[-5:]:
            print(f"      | {ln[:160]}")
    return code, out


# ---------------------------------------------------------------- 环境探测

def find_repo(start: Path) -> Path:
    for base in (start, *start.parents, Path.cwd(), *Path.cwd().parents):
        cand = base / "scholar-agent" if (base / "scholar-agent").is_dir() else base
        if (cand / "api" / "pyproject.toml").is_file() and (cand / "web").is_dir():
            return cand
    raise SystemExit("找不到仓库：请用 --repo 指定 scholar-agent 根目录")


def find_python(api: Path) -> str:
    if os.name == "nt":
        cand = api / ".venv" / "Scripts" / "python.exe"
    else:
        cand = api / ".venv" / "bin" / "python"
    return str(cand) if cand.is_file() else sys.executable


def find_node() -> str | None:
    for name in ("node", "node.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (r"C:\Program Files\nodejs\node.exe", "/usr/bin/node", "/usr/local/bin/node"):
        if Path(cand).is_file():
            return cand
    return None


def free_port(default: int = 18100) -> int:
    for port in range(default, default + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return default


# ---------------------------------------------------------------- 各步骤

def step_git(ctx: Ctx, strict: bool) -> None:
    code, out = run(ctx, "git_status", ["git", "status", "--porcelain"])
    dirty = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("$")]
    if not dirty:
        ctx.add(Result("git 工作区", "PASS", "干净"))
    else:
        ctx.add(Result("git 工作区", "FAIL" if strict else "WARN",
                       f"{len(dirty)} 项未提交（--strict-git 时为 FAIL）"))
    code, out = run(ctx, "git_sync", ["git", "rev-list", "--left-right", "--count",
                                      "origin/main...HEAD"])
    m = re.search(r"(\d+)\s+(\d+)", out)
    if m and m.group(1) == "0" and m.group(2) == "0":
        ctx.add(Result("git 已推送", "PASS", "与 origin/main 同步"))
    else:
        ctx.add(Result("git 已推送", "FAIL" if strict else "WARN", out.strip()[:100]))
    code, out = run(ctx, "git_tags", ["git", "tag", "--points-at", "HEAD"])
    tags = [t for t in out.split() if re.match(r"^v?\d", t)]
    ctx.add(Result("HEAD 有验收 tag", "PASS" if tags else "SKIP",
                   ",".join(tags) if tags else "未打 tag（建议验收后 git tag v0.5.0）"))


def step_ruff(ctx: Ctx) -> None:
    code, out = run(ctx, "ruff", [ctx.py, "-m", "ruff", "check", "app", "tests"], cwd=ctx.api)
    ctx.add(Result("ruff 静态检查", "PASS" if code == 0 else "FAIL",
                   "All checks passed" if code == 0 else "见 ruff.log"))


def step_pytest(ctx: Ctx, min_tests: int) -> None:
    code, out = run(ctx, "pytest", [ctx.py, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    cwd=ctx.api, timeout=2400)
    m = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    passed = int(m.group(1)) if m else 0
    failed = int(f.group(1)) if f else (0 if code == 0 else 1)
    ok = failed == 0 and passed >= min_tests
    ctx.add(Result("pytest 全量", "PASS" if ok else "FAIL",
                   f"{passed} passed / {failed} failed（下限 {min_tests}）"))


def step_p0_regressions(ctx: Ctx, min_p0: int) -> None:
    """P0 回归测试存在性：统计 api/tests 下 def test_p0_* / def test_p1_* 数量。"""
    count = 0
    files = []
    for p in (ctx.api / "tests").glob("test_*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        n = len(re.findall(r"^\s*def (test_p0_|test_p1_)", text, re.M))
        if n:
            files.append(f"{p.name}:{n}")
            count += n
    ok = count >= min_p0
    ctx.add(Result("P0 回归测试", "PASS" if ok else "FAIL",
                   f"{count} 条（下限 {min_p0}）· {' '.join(files[:6])}"))


def step_official_cases(ctx: Ctx, skip: bool) -> None:
    if skip:
        ctx.add(Result("官方 3 用例", "SKIP", "--skip-cases"))
        return
    tmp = ctx.outdir / "tmp_cases"
    env = {
        "SCHOLAR_DATA_DIR": str(tmp),
        "MINERU_ENABLED": "off", "VLM_ENABLED": "0",
        "LLM_PROVIDER": "", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        "DEEPSEEK_API_KEY": "", "MINIMAX_API_KEY": "", "DASHSCOPE_API_KEY": "",
        "EMBEDDING_BACKEND": "off", "EMBEDDING_API_KEY": "", "SEMANTIC_JUDGE": "0",
    }
    code, out = run(ctx, "official_cases",
                    [ctx.py, "scripts/run_official_cases.py"], cwd=ctx.api,
                    env=env, timeout=3600)
    m = re.search(r"汇总：\s*(\d+)/(\d+)\s*PASS", out)
    ok = bool(m and m.group(1) == m.group(2) == "3")
    detail = m.group(0) if m else "未解析到汇总行，见日志"
    ctx.add(Result("官方 3 用例（容器等价：引擎关）", "PASS" if ok else "FAIL", detail))


def step_static_redlines(ctx: Ctx) -> None:
    dockerfile = (ctx.api / "Dockerfile").read_text(encoding="utf-8", errors="replace")
    has_uvlock = "uv.lock" in dockerfile
    ctx.add(Result("Dockerfile 含 uv.lock", "PASS" if has_uvlock else "FAIL",
                   "P0-4" if not has_uvlock else "ok"))

    gi = (ctx.repo / ".gitignore").read_text(encoding="utf-8", errors="replace")
    ignore_ok = "_prs_" in gi
    ctx.add(Result(".gitignore 覆盖测试临时目录", "PASS" if ignore_ok else "FAIL",
                   "_prs_*" if ignore_ok else "缺 _prs_*"))

    code, out = run(ctx, "git_tracked_env", ["git", "ls-files"])
    tracked_env = [ln for ln in out.splitlines() if ln.strip() in (".env", "api/.env")]
    ctx.add(Result(".env 未被跟踪", "PASS" if not tracked_env else "FAIL",
                   "ok" if not tracked_env else f"泄露：{tracked_env}"))


def _node_bin(ctx: Ctx, rel: str) -> list[str] | None:
    if not ctx.node:
        return None
    p = ctx.web / rel
    return [ctx.node, str(p)] if p.is_file() else None


def step_web(ctx: Ctx, skip: bool) -> None:
    if skip:
        for n in ("tsc", "next lint", "next build"):
            ctx.add(Result(f"web {n}", "SKIP", "--skip-web"))
        return
    if not (ctx.web / "node_modules").is_dir():
        for n in ("tsc", "next lint", "next build"):
            ctx.add(Result(f"web {n}", "SKIP", "web/node_modules 不存在，先 pnpm install"))
        return
    tsc = _node_bin(ctx, "node_modules/typescript/bin/tsc")
    if tsc:
        code, _ = run(ctx, "web_tsc", [*tsc, "--noEmit"], cwd=ctx.web, timeout=600)
        ctx.add(Result("web tsc --noEmit", "PASS" if code == 0 else "FAIL"))
    else:
        ctx.add(Result("web tsc --noEmit", "SKIP", "未找到 typescript"))
    nxt = _node_bin(ctx, "node_modules/next/dist/bin/next")
    if nxt:
        code, _ = run(ctx, "web_lint", [*nxt, "lint"], cwd=ctx.web, timeout=600)
        ctx.add(Result("web next lint", "PASS" if code == 0 else "FAIL"))
        code, _ = run(ctx, "web_build", [*nxt, "build"], cwd=ctx.web, timeout=1800)
        ctx.add(Result("web next build", "PASS" if code == 0 else "FAIL"))
    else:
        ctx.add(Result("web lint/build", "SKIP", "未找到 next"))


def step_docker(ctx: Ctx, skip: bool) -> None:
    if skip:
        ctx.add(Result("docker build", "SKIP", "--skip-docker"))
        return
    if not shutil.which("docker"):
        ctx.add(Result("docker build api", "SKIP", "本机无 docker"))
        ctx.add(Result("docker build web", "SKIP", "本机无 docker"))
        return
    code, out = run(ctx, "docker_info", ["docker", "info", "--format", "{{.ServerVersion}}"],
                    timeout=60)
    if code != 0:
        ctx.add(Result("docker build api", "SKIP", "docker daemon 不可用"))
        ctx.add(Result("docker build web", "SKIP", "docker daemon 不可用"))
        return
    code, _ = run(ctx, "docker_build_api", ["docker", "build", "-t", "scholar-api:accept", "api"],
                  cwd=ctx.repo, timeout=2400)
    ctx.add(Result("docker build api", "PASS" if code == 0 else "FAIL"))
    code, _ = run(ctx, "docker_build_web",
                  ["docker", "build", "-t", "scholar-web:accept", "web"], cwd=ctx.repo,
                  timeout=2400)
    ctx.add(Result("docker build web", "PASS" if code == 0 else "FAIL"))


def step_smoke(ctx: Ctx, skip: bool) -> None:
    if skip:
        ctx.add(Result("真 HTTP smoke", "SKIP", "--skip-smoke"))
        return
    port = free_port()
    tmp = ctx.outdir / "tmp_smoke"
    env = os.environ.copy()
    env.update({
        "SCHOLAR_DATA_DIR": str(tmp), "MINERU_ENABLED": "off", "VLM_ENABLED": "0",
        "EMBEDDING_BACKEND": "off", "EMBEDDING_API_KEY": "",
        "LLM_PROVIDER": "", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        "DEEPSEEK_API_KEY": "", "MINIMAX_API_KEY": "", "DASHSCOPE_API_KEY": "",
    })
    log = ctx.log_path("uvicorn")
    f = log.open("w", encoding="utf-8")
    proc = subprocess.Popen([ctx.py, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=str(ctx.api), env=env, stdout=f, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        url = f"http://127.0.0.1:{port}/api/health"
        up = False
        for _ in range(90):
            try:
                urllib.request.urlopen(url, timeout=2)
                up = True
                break
            except Exception:
                time.sleep(1)
        if not up:
            ctx.add(Result("真 HTTP smoke", "FAIL", "API 60~90s 内未就绪，见 uvicorn.log"))
            return
        code, out = run(ctx, "smoke",
                        [ctx.py, "scripts/smoke_web.py"],
                        cwd=ctx.api,
                        env={"API_BASE": f"http://127.0.0.1:{port}"},
                        timeout=600)
        m = re.search(r"冒烟：\s*(\d+)/(\d+)\s*PASS", out)
        ok = bool(m and m.group(1) == m.group(2) == "9")
        extra = ""
        if not ok and m:
            extra = f"（{m.group(1)}/{m.group(2)}）"
        ctx.add(Result("真 HTTP smoke（9 项）", "PASS" if ok else "FAIL",
                       (m.group(0) if m else "未解析到汇总行") + extra))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        f.close()


# ---------------------------------------------------------------- 汇总

def write_summary(ctx: Ctx) -> int:
    fail = [r for r in ctx.results if r.status == "FAIL"]
    skip = [r for r in ctx.results if r.status == "SKIP"]
    lines = ["# Round2 验收报告", "",
             f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 仓库：`{ctx.repo}`",
             f"- 命令：`{' '.join(sys.argv)}`", "",
             "## 结果", "", "| 步骤 | 结果 | 说明 |", "|---|---|---|"]
    for r in ctx.results:
        lines.append(f"| {r.name} | **{r.status}** | {r.detail} |")
    lines += ["", f"**总计：{len(ctx.results) - len(fail) - len(skip)} PASS / "
                  f"{len(fail)} FAIL / {len(skip)} SKIP**", ""]
    (ctx.outdir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    (ctx.outdir / "summary.json").write_text(
        json.dumps([r.__dict__ for r in ctx.results], ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"汇总：{len(ctx.results) - len(fail) - len(skip)} PASS / "
          f"{len(fail)} FAIL / {len(skip)} SKIP")
    if fail:
        print("FAIL 项：" + "、".join(r.name for r in fail))
    print(f"报告：{ctx.outdir / 'summary.md'}")
    print("=" * 72)
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Round2 一键验收")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--full", action="store_true", help="追加 web/docker/smoke")
    ap.add_argument("--strict-git", action="store_true")
    ap.add_argument("--min-tests", type=int, default=109)
    ap.add_argument("--min-p0-tests", type=int, default=7)
    ap.add_argument("--skip-cases", action="store_true")
    ap.add_argument("--skip-smoke", action="store_true")
    ap.add_argument("--skip-docker", action="store_true")
    ap.add_argument("--skip-web", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else find_repo(Path(__file__).resolve().parent)
    api, web = repo / "api", repo / "web"
    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = RUNS_DIR / ts
    outdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(repo=repo, api=api, web=web, py=find_python(api), node=find_node(),
              outdir=outdir, args=args)
    print(f"仓库：{repo}\nPython：{ctx.py}\nNode：{ctx.node}\n输出：{outdir}\n" + "-" * 72)

    step_git(ctx, args.strict_git)
    step_static_redlines(ctx)
    step_ruff(ctx)
    step_pytest(ctx, args.min_tests)
    step_p0_regressions(ctx, args.min_p0_tests)
    step_official_cases(ctx, args.skip_cases)
    if args.full:
        step_web(ctx, args.skip_web)
        step_docker(ctx, args.skip_docker)
        step_smoke(ctx, args.skip_smoke)
    return write_summary(ctx)


if __name__ == "__main__":
    sys.exit(main())
