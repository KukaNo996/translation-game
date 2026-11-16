"""真实游戏基准测试：启动游戏 → 抓真实画面 → 量 OCR / 翻译 / 渲染开销。

与 tests/verify_live.py 的区别：那个用 tools/fake_game.py 的假窗口，
这个直接吃真实游戏（Unity/IL2CPP、真实字体、真实配色、真动画）。

用法：
    python tools/bench_game.py --exe "E:/Game/xxx/Game.exe" --title Summer --frames 12
    python tools/bench_game.py --hwnd 123456 --frames 6          # 已在运行的游戏
输出：
    GameLensData/bench_report.json   +  tests/assets/real/*.png（帧截图）
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gamelens import capture, ocr as ocr_mod, winapi          # noqa: E402
from gamelens.translate import TranslationService             # noqa: E402
from gamelens.config import Config                            # noqa: E402

OUT_DIR = os.path.join(ROOT, "tests", "assets", "real")
REPORT = os.path.join(ROOT, "GameLensData", "bench_report.json")


# ---------------------------------------------------------------- 启动 / 等待
def launch(exe: str, args=None):
    cwd = os.path.dirname(exe)
    p = subprocess.Popen([exe] + list(args or []), cwd=cwd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def wait_window(pid: int, title: str = "", timeout: float = 90.0):
    end = time.time() + timeout
    while time.time() < end:
        for w in winapi.list_windows(min_size=200):
            if w.pid == pid and (not title or title.lower() in w.title.lower()):
                return w
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------- 计时工具
def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    return out, (time.perf_counter() - t0) * 1000.0


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * p))))
    return vals[k]


def summarize(name: str, vals):
    return {"name": name, "n": len(vals),
            "avg": round(statistics.mean(vals), 1) if vals else 0,
            "p50": round(pct(vals, 0.5), 1), "p90": round(pct(vals, 0.9), 1),
            "max": round(max(vals), 1) if vals else 0}


def _kill(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _write(report) -> None:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def _print_summary(report) -> None:
    print("\n=== 汇总 ===")
    for k, v in report.get("summary", {}).items():
        print(" ", k, v)
    p = report.get("pipeline")
    if p:
        print(f"  流水线 {p['frames']} 帧 / {p['seconds']}s："
              f"单帧 OCR 中位 {p['ocr_p50']}ms，整帧中位 {p['took_p50']}ms，"
              f"上屏 {p['sprites_avg']} 条，待译 {p['pending_avg']}")
        for line in p.get("log", [])[-6:]:
            print("   ·", line)
    print(f"\n报告：{REPORT}")


def run_pipeline(cfg, svc, hwnd: int, seconds: float, log=print,
                 legacy_ocr: bool = False) -> dict:
    """把真实流水线跑起来，统计"一帧到底花多久"——这是玩家真正感受到的延迟。"""
    from dataclasses import replace

    from gamelens import ocr as O
    from gamelens.pipeline import Pipeline

    if legacy_ocr:
        O.choose_engine = lambda pref, log=None, source_lang="en": O.RapidOcr(fast=False)

    cfg.set("mode", "region")
    cfg.set("region_hwnd", hwnd)
    cfg.set("ocr_engine", "auto")
    rect = winapi.client_rect_on_screen(hwnd)
    if not rect:
        raise SystemExit("拿不到窗口客户区")

    samples, logs = [], []
    pl = Pipeline(cfg, svc, lambda b, s: None, lambda st: samples.append(replace(st)),
                  lambda m: (logs.append(m), log(m)))
    pl.start("region", rect, hwnd)
    time.sleep(seconds)
    pl.stop()

    ocr = [s.ocr_ms for s in samples if s.ocr_ms]
    took = [s.took_ms for s in samples if s.took_ms]
    sprites = [s.sprites for s in samples]
    pending = [s.pending for s in samples]
    return {
        "seconds": seconds, "frames": len(samples),
        "ocr_p50": round(pct(ocr, 0.5), 1) if ocr else 0,
        "ocr_p90": round(pct(ocr, 0.9), 1) if ocr else 0,
        "took_p50": round(pct(took, 0.5), 1) if took else 0,
        "took_p90": round(pct(took, 0.9), 1) if took else 0,
        "sprites_avg": round(statistics.mean(sprites), 1) if sprites else 0,
        "pending_avg": round(statistics.mean(pending), 1) if pending else 0,
        "engine": next((s.engine for s in reversed(samples) if s.engine), ""),
        "legacy_ocr": legacy_ocr,
        "log": logs,
    }


def run_verify(cfg, svc, hwnd: int, seconds: float, log=print) -> dict:
    """真机验收：开真界面 + 真叠加层，对着真游戏跑一帧，截图存证。"""
    from PIL import ImageGrab

    from gamelens.app import App

    rect = winapi.client_rect_on_screen(hwnd)
    if not rect:
        raise SystemExit("拿不到窗口客户区")

    app = App()
    logs, chinese = [], []
    base_log = app.log
    app.log = lambda m: (logs.append(m), base_log(m))
    app.cfg.set("provider", "litemt")
    app.cfg.set("interval_ms", 350)
    app.cfg.set("region", list(rect))
    app.cfg.set("region_hwnd", hwnd)
    app.service.invalidate()
    app._select_mode("region")
    app._start()
    app.update()
    app.withdraw()                       # 主界面让开，画面里只留游戏 + 叠加层

    deadline = time.time() + seconds
    sprites = []
    while time.time() < deadline:
        app.update()
        sprites = app.overlay._last_payload or []
        # 等到"真的像一屏可读的界面"再截图：光靠条数会在启动 logo 上就提前收工
        zh = [sp.text for sp in sprites if any("\u4e00" <= c <= "\u9fff" for c in sp.text)]
        if len(zh) >= 3 and len(sprites) >= 3:
            break
        time.sleep(0.2)
    time.sleep(0.6)
    app.update()
    sprites = app.overlay._last_payload or []
    chinese = [sp.text for sp in sprites]

    shot_on = os.path.join(OUT_DIR, "real_translated.png")
    img_on = ImageGrab.grab(bbox=rect)
    img_on.save(shot_on)

    app._stop()
    app.update()
    time.sleep(0.5)
    shot_off = os.path.join(OUT_DIR, "real_original.png")
    img_off = ImageGrab.grab(bbox=rect)
    img_off.save(shot_off)
    try:
        app.destroy()
    except Exception:
        pass

    changed = 0.0
    before_text, after_text = [], []
    try:
        import numpy as np
        a = np.asarray(img_on.convert("L"), dtype=np.int16)
        b = np.asarray(img_off.convert("L"), dtype=np.int16)
        if a.shape == b.shape:
            changed = float((np.abs(a - b) > 24).mean()) * 100
        eng = ocr_mod.RapidOcr()
        before_text = [l.text for l in eng.recognize(img_off)]
        after_text = [l.text for l in eng.recognize(img_on)]
    except Exception as exc:
        log(f"对照统计失败：{exc}")

    return {"hwnd": hwnd, "rect": list(rect), "sprites": len(sprites),
            "chinese": chinese, "pixel_changed_pct": round(changed, 2),
            "before_text": before_text, "after_text": after_text,
            "shot_on": shot_on, "shot_off": shot_off,
            "engine": next((s for s in logs if "OCR 引擎" in s), ""),
            "log": logs[-12:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--hwnd", type=int, default=0)
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--interval", type=float, default=0.7)
    ap.add_argument("--engines", default="winrt,rapid")
    ap.add_argument("--save", action="store_true", help="保存帧截图")
    ap.add_argument("--pipeline", action="store_true",
                    help="跑真实流水线（截图→OCR→翻译→上屏）并统计每帧耗时")
    ap.add_argument("--verify", action="store_true",
                    help="开真界面开真叠加层，把译文真的画到游戏上并截图存证")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--warmup", type=float, default=0.0,
                    help="先等游戏进入主界面再开始计时（跳过加载动画）")
    ap.add_argument("--lang", default="", help="源语言：en / ja / auto")
    ap.add_argument("--legacy-ocr", action="store_true",
                    help="按改动前的 RapidOCR 参数跑（角度分类器开、det 不缩边），用于前后对比")
    a = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    winapi.enable_dpi_awareness()

    proc = None
    if a.hwnd:
        hwnd = a.hwnd
    else:
        if not a.exe:
            print("需要 --exe 或 --hwnd")
            return 2
        print(f"启动 {a.exe}")
        proc = launch(a.exe)
        w = wait_window(proc.pid, a.title)
        if not w:
            print("没等到游戏窗口（可能启动失败/需要额外运行库）")
            try:
                proc.kill()
            except Exception:
                pass
            return 3
        hwnd = w.hwnd
        print(f"窗口 OK: {w.title!r} {w.rect}")

    report = {"exe": a.exe, "hwnd": hwnd, "frames": [], "summary": {}}
    cfg = Config()
    if a.lang:
        cfg.set("source_lang", a.lang)
        cfg.save()
    svc = TranslationService(cfg)

    engines = {}
    if a.verify:
        r = run_verify(cfg, svc, hwnd, a.seconds, log=print)
        report["verify"] = r
        _write(report)
        print(f"\n=== 上屏验证 ===\n  叠加层画出 {r['sprites']} 条译文：{r['chinese']}")
        print(f"  译文区域像素变化 {r['pixel_changed_pct']}%")
        print(f"  截图：{r['shot_on']}\n  原图：{r['shot_off']}")
        if proc:
            _kill(proc)
        return 0
    if a.pipeline:
        if a.warmup:
            time.sleep(a.warmup)
        r = run_pipeline(cfg, svc, hwnd, a.seconds, log=print, legacy_ocr=a.legacy_ocr)
        report["pipeline"] = r
        _write(report)
        _print_summary(report)
        if proc:
            _kill(proc)
        return 0

    for name in [e.strip() for e in a.engines.split(",") if e.strip()]:
        t0 = time.perf_counter()
        try:
            engines[name] = ocr_mod.WinRtOcr() if name == "winrt" else ocr_mod.RapidOcr()
            print(f"引擎 {name} 就绪 {int((time.perf_counter()-t0)*1000)} ms -> {engines[name].label}")
        except Exception as exc:
            print(f"引擎 {name} 不可用：{exc}")

    times = {k: [] for k in engines}
    grab_ms, diff_ms = [], []
    hasher = capture.FrameHasher()
    prev_img = None
    all_text, all_zh = [], []
    frames_meta = []

    for i in range(a.frames):
        img, g = timed(capture.grab_window, hwnd)
        if img is None:
            print(f"帧 {i}: 抓不到画面，跳过")
            time.sleep(0.4)
            continue
        grab_ms.append(g)
        rec = {"i": i, "grab_ms": round(g, 1), "size": list(img.size)}
        if a.save:
            img.save(os.path.join(OUT_DIR, f"frame_{i:02d}.png"))

        if prev_img is not None and prev_img.size == img.size:
            from gamelens import pipeline as pl
            _, d = timed(pl._diff_box, prev_img, img, 14)
            diff_ms.append(d)
            rec["diff_ms"] = round(d, 1)
        prev_img = img.copy()

        hasher.changed(img)
        lines_by_engine = {}
        for name, eng in engines.items():
            lines, ms = timed(eng.recognize, img)
            times[name].append(ms)
            lines_by_engine[name] = lines
            rec[name] = {"ms": round(ms, 1), "raw": len(lines)}

        # 用第一个引擎的结果走完整合并+过滤+翻译
        base = next(iter(lines_by_engine.values())) if lines_by_engine else []
        merged = ocr_mod.merge_into_lines(base)
        groups = ocr_mod.group_paragraphs(merged)
        groups = [g for g in groups if g.text.strip()]
        texts = [g.text for g in groups]
        all_text.extend(texts)

        t_ms = 0.0
        zh_map = {}
        if texts:
            res, t_ms = timed(svc.translate_batch, texts)
            for s, z in zip(texts, res):
                zh_map[s] = z
                if z:
                    all_zh.append(s)
        rec.update({"groups": len(groups), "translated": len(zh_map) and
                    sum(1 for v in zh_map.values() if v),
                    "tr_ms": round(t_ms, 1)})
        frames_meta.append(rec)
        print(f"帧 {i}: 抓图 {g:.0f}ms  " + "  ".join(
            f"{n}={rec[n]['ms']:.0f}ms/{len(lines_by_engine[n])}行" for n in engines)
            + f"  组段 {len(groups)}  汉化 {rec['translated']}  翻译 {t_ms:.0f}ms")
        time.sleep(a.interval)

    report["frames"] = frames_meta
    report["summary"] = {
        "grab": summarize("grab_window", grab_ms),
        "diff": summarize("frame_diff", diff_ms),
        **{k: summarize(k, v) for k, v in times.items()},
        "coverage": round(len(all_zh) / max(1, len(all_text)), 3),
        "lines_total": len(all_text),
        "lines_translated": len(all_zh),
    }
    report["ocr_text"] = sorted(set(all_text))

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("\n=== 汇总 ===")
    for k, v in report["summary"].items():
        print(" ", k, v)
    print(f"\n汉化覆盖率 {report['summary']['coverage']:.1%}  "
          f"({report['summary']['lines_translated']}/{report['summary']['lines_total']} 段)")
    print(f"报告：{REPORT}")

    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
