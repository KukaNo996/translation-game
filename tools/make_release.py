"""一键产出发行版：清理 → PyInstaller 打包 → 自检 → 压缩 → 校验和 →（可选）上传。

用法：
    python tools/make_release.py                 # 只打包，产物在 release/
    python tools/make_release.py --skip-build    # 不重新打包，只用现成的 exe 做校验/压缩
    python tools/make_release.py --upload github # 打包并上传到 GitHub Release
    python tools/make_release.py --upload all    # GitHub + Gitee 都传

令牌只从环境变量读（GITHUB_TOKEN / GITEE_TOKEN），不接受命令行参数，
避免令牌进 shell 历史和进程列表。CI 里由 GitHub 自动注入 GITHUB_TOKEN。

产物：
    release/GameLens.exe
    release/GameLens-v<版本>-win64.zip      （exe + README + LICENSE）
    release/SHA256SUMS.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RELEASE = ROOT / "release"
EXE = RELEASE / "GameLens.exe"
DIST_REPO = "translation-game"
GH_OWNER = os.environ.get("GH_OWNER", "KukaNo996")
GITEE_OWNER = os.environ.get("GITEE_OWNER", "")


def version() -> str:
    """版本号唯一来源：src/gamelens/__init__.py 的 __version__。"""
    text = (SRC / "gamelens" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("src/gamelens/__init__.py 里找不到 __version__")
    return m.group(1)


def run(cmd, **kw) -> int:
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.call(cmd, **kw)


def build() -> None:
    for d in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(d, ignore_errors=True)
    RELEASE.mkdir(exist_ok=True)
    if EXE.exists():
        EXE.unlink()
    code = run([sys.executable, "-m", "PyInstaller", "build.spec",
                "--noconfirm", "--distpath", str(RELEASE),
                "--workpath", str(ROOT / "build")], cwd=str(ROOT))
    if code != 0 or not EXE.exists():
        raise SystemExit("打包失败")


def selftest() -> bool:
    """跑 exe 自带自检。

    本机 release/GameLensData 里可能有开发者自己配的在线翻译服务（甚至是失效的），
    自检会照着那份配置去连、白等超时。发行版要验的是"全新用户第一次启动"，
    所以先把这份本地配置挪开，跑完再放回去。
    """
    if not EXE.exists():
        return False
    cfg_dir = RELEASE / "GameLensData"
    backup = RELEASE / "_local_cfg_backup"
    moved = False
    try:
        if cfg_dir.exists():
            shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(cfg_dir), str(backup))
            moved = True
        out = subprocess.run([str(EXE), "--selftest"], cwd=str(RELEASE),
                             capture_output=True, timeout=600)
        text = (out.stdout or b"").decode("utf-8", "ignore")
        ok = out.returncode == 0
        tail = [ln for ln in text.splitlines() if ln.strip()][-6:]
        print("\n".join(tail) if tail else "(自检无输出)")
        return ok
    except Exception as exc:
        print(f"自检未通过（继续打包）：{exc}")
        return False
    finally:
        if moved:
            shutil.rmtree(cfg_dir, ignore_errors=True)
            shutil.move(str(backup), str(cfg_dir))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_zip(ver: str) -> Path:
    out = RELEASE / f"GameLens-v{ver}-win64.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(EXE, "GameLens.exe")
        for name in ("README.md", "LICENSE"):
            p = ROOT / name
            if p.exists():
                z.write(p, name)
    return out


def write_sums(paths) -> Path:
    p = RELEASE / "SHA256SUMS.txt"
    lines = [f"{sha256(f)}  {f.name}" for f in paths]
    p.write_text("\n".join(lines) + "\n", "utf-8")
    print("\n".join(lines))
    return p


# ---------------------------------------------------------------- 上传
def _http_json(url: str, token: str, method: str = "GET", body=None):
    import requests
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    r = requests.request(method, url, headers=headers, json=body, timeout=60)
    return r


def upload_github(ver: str, assets) -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("跳过 GitHub：没有 GITHUB_TOKEN 环境变量")
        return
    tag = f"v{ver}"
    base = f"https://api.github.com/repos/{GH_OWNER}/{DIST_REPO}"
    rel = _http_json(f"{base}/releases/tags/{tag}", token)
    if rel.status_code == 404:
        body = {"tag_name": tag, "name": f"GameLens {tag}",
                "body": f"GameLens {tag}\n\nWindows 10/11 单文件版，双击即用，无需安装 Python。",
                "draft": False, "prerelease": False}
        rel = _http_json(f"{base}/releases", token, "POST", body)
    if rel.status_code >= 300:
        print(f"GitHub Release 创建失败：{rel.status_code} {rel.text[:300]}")
        return
    rid = rel.json()["id"]
    upload_url = rel.json()["upload_url"].split("{")[0]
    import requests
    for f in assets:
        print(f"上传 {f.name}（{f.stat().st_size / 1048576:.1f} MB）…")
        with open(f, "rb") as fh:
            r = requests.post(
                f"{upload_url}?name={f.name}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/octet-stream"},
                data=fh, timeout=1800)
        print("  ", r.status_code, "ok" if r.status_code < 300 else r.text[:200])


def upload_gitee(ver: str, assets) -> None:
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token:
        print("跳过 Gitee：没有 GITEE_TOKEN 环境变量")
        return
    if not GITEE_OWNER:
        print("跳过 Gitee：没有 GITEE_OWNER 环境变量")
        return
    import requests
    tag = f"v{ver}"
    api = f"https://gitee.com/api/v5/repos/{GITEE_OWNER}/{DIST_REPO}"
    r = requests.post(f"{api}/releases", timeout=60, data={
        "access_token": token, "tag_name": tag, "name": f"GameLens {tag}",
        "body": f"GameLens {tag} 源码版；可执行文件见 GitHub Releases。",
        "target_commitish": "main"})
    if r.status_code >= 300:
        print(f"Gitee Release 创建失败：{r.status_code} {r.text[:300]}")
        return
    rid = r.json()["id"]
    for f in assets:
        # Gitee 免费版附件上限 100MB，超了就别传，避免卡半小时再失败
        if f.stat().st_size > 100 * 1024 * 1024:
            print(f"跳过 {f.name}：超过 Gitee 100MB 附件上限")
            continue
        print(f"上传 {f.name} 到 Gitee…")
        with open(f, "rb") as fh:
            rr = requests.post(f"{api}/releases/{rid}/attach_files", timeout=1800,
                               data={"access_token": token}, files={"file": fh})
        print("  ", rr.status_code, "ok" if rr.status_code < 300 else rr.text[:200])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--upload", choices=["", "github", "gitee", "all"], default="")
    a = ap.parse_args()

    ver = version()
    print(f"=== GameLens v{ver} 发行版打包 ===")

    if not a.skip_build:
        build()
    if not EXE.exists():
        raise SystemExit(f"找不到 {EXE}")

    size_mb = EXE.stat().st_size / 1048576
    print(f"exe: {EXE}  {size_mb:.1f} MB")
    if size_mb < 30:
        raise SystemExit("exe 体积异常（<30MB），可能缺少 OCR 模型依赖，已中止")

    ok = selftest()
    print(f"自检：{'通过' if ok else '未通过（不影响出包）'}")

    zp = make_zip(ver)
    sums = write_sums([EXE, zp])
    print(f"\n产物：\n  {EXE}\n  {zp}\n  {sums}")

    if a.upload in ("github", "all"):
        upload_github(ver, [EXE, zp, sums])
    if a.upload in ("gitee", "all"):
        upload_gitee(ver, [EXE, zp, sums])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
