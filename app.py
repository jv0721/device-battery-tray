# -*- coding: utf-8 -*-
"""app.py —— 打包成 exe 之后的统一入口。

一个 exe 同时当托盘和当面板，靠参数分工：

    （双击，不带参数）  托盘没在跑就起托盘，然后打开面板
    --tray              只起托盘（开机自启走这个，不会弹面板）
    --panel             只开面板
    --status / --quit / --autostart on|off / --install
                        原样交给托盘那边处理

从源码跑的时候仍然可以分别用 `python tray_app.py` 和 `python my_devices.py`，
这个文件只是给 exe 用的门面。
"""
from __future__ import annotations

import sys


def _probe() -> int:
    """开一次面板，把画布状态和所有异常写到 exe 旁边的 probe.txt。

    专门用来排查「无控制台版本」的静默故障 —— 那种情况什么都看不到。
    """
    import json
    import os
    import traceback

    import tkinter

    import tray_common as tc

    errors = []
    dump = {}

    dump["res_dir"] = tc.RES_DIR
    dump["meipass"] = getattr(sys, "_MEIPASS", None)
    adir = os.path.join(tc.RES_DIR, "assets")
    dump["assets"] = (sorted(os.listdir(adir)) if os.path.isdir(adir)
                      else "目录不存在")
    dump["env_pyi"] = {k: v for k, v in os.environ.items()
                       if k.startswith("_PYI") or "_MEIPASS" in k.upper()}
    dump["temp_mei"] = sorted(
        os.path.basename(d) for d in
        [os.path.join(__import__("tempfile").gettempdir(), n)
         for n in os.listdir(__import__("tempfile").gettempdir())
         if n.startswith("_MEI")])

    def report(self, exc, val, tb):
        errors.append("".join(traceback.format_exception(exc, val, tb)))

    tkinter.Tk.report_callback_exception = report

    def snapshot(app):
        try:
            cv = getattr(app, "cv", None)
            dump["canvas_items"] = len(cv.find_all()) if cv is not None else "没画布"
            dump["cv_size"] = (cv.winfo_width(), cv.winfo_height()) if cv else None
            dump["win_size"] = (app.winfo_width(), app.winfo_height())

            def desc(v):
                if v is None:
                    return "空"
                try:
                    return "tk %dx%d" % (v.width(), v.height())
                except TypeError:
                    return "PIL %sx%s" % (v.width, v.height)
                except Exception as e:
                    return "其他 %r" % (e,)

            dump["imgs"] = {k: desc(v) for k, v in getattr(app, "imgs", {}).items()}
            dump["img_err"] = getattr(app, "img_err", {})
            dump["pil_last_err"] = getattr(sys.modules.get("my_devices"), "LAST_ERR", "")
            try:
                got = app.pic("a7", 400, 300)
                dump["pic_a7"] = desc(got) if got is not None else "None（→占位）"
            except Exception:
                dump["pic_a7"] = traceback.format_exc()
            dump["data"] = repr(getattr(app, "data", None))[:400]
            dump["theme"] = getattr(app, "theme", "?")
        except Exception:
            dump["snapshot_err"] = traceback.format_exc()

    orig_loop = tkinter.Tk.mainloop

    def snap_n(app, tag):
        try:
            cv = getattr(app, "cv", None)
            dump.setdefault("steps", []).append({
                "at": tag,
                "cv": [cv.winfo_width(), cv.winfo_height()] if cv else None,
                "win": [app.winfo_width(), app.winfo_height()],
                "items": len(cv.find_all()) if cv else 0,
                "mapped": bool(app.winfo_ismapped()),
            })
        except Exception:
            dump.setdefault("steps", []).append({"at": tag, "err": traceback.format_exc()})

    def patched_loop(self, *a, **kw):
        snapshot(self)
        for ms in (300, 1200, 2200):
            try:
                self.after(ms, lambda m=ms: snap_n(self, m))
            except Exception:
                pass
        return orig_loop(self, *a, **kw)

    tkinter.Tk.mainloop = patched_loop

    try:
        import my_devices
        dump["_PIL"] = my_devices._PIL
        my_devices.run_gui(autoclose_ms=2500)
    except Exception:
        errors.append(traceback.format_exc())

    path = os.path.join(tc.BASE_DIR, "probe.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"dump": dump, "errors": errors}, ensure_ascii=False,
                           indent=2))
    return 0


def _strip(flag: str, args):
    return [a for a in args if a != flag]


def _diag() -> int:
    """把冻结环境下的关键路径写到 exe 旁边的 diag.txt（打包后排查用）。"""
    import json
    import os

    import tray_common as tc

    out = {
        "frozen": tc.FROZEN,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "meipass": getattr(sys, "_MEIPASS", None),
        "base_dir": tc.BASE_DIR,
        "res_dir": tc.RES_DIR,
        "config": tc.CONFIG_PATH,
        "assets": os.path.join(tc.RES_DIR, "assets"),
    }
    adir = out["assets"]
    out["assets_list"] = sorted(os.listdir(adir)) if os.path.isdir(adir) else "目录不存在"
    out["png"] = {n: os.path.exists(os.path.join(adir, n))
                  for n in ("keyboard.png", "mouse.png", "a7.png")}
    try:
        import my_devices
        out["_PIL"] = my_devices._PIL
    except Exception as e:
        out["_PIL"] = "导入失败：%r" % (e,)
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        ok = {}
        for n in ("keyboard.png", "mouse.png", "a7.png"):
            try:
                im = tk.PhotoImage(file=os.path.join(adir, n))
                ok[n] = "%dx%d" % (im.width(), im.height())
            except Exception as e:
                ok[n] = "失败：%r" % (e,)
        out["tk_load"] = ok
        root.destroy()
    except Exception as e:
        out["tk_load"] = "tk 起不来：%r" % (e,)

    # 图片链路逐段体检：打开 → 缩放 → 存成 PNG → 作为数据交给 tk
    import base64 as _b64
    import io as _io
    import traceback as _tb
    chain = {}
    buf = None
    try:
        from PIL import Image as _Image
        im = _Image.open(os.path.join(adir, "a7.png")).convert("RGBA")
        chain["open"] = "ok %dx%d" % im.size
        sm = im.resize((60, 31), getattr(_Image, "Resampling", _Image).LANCZOS)
        chain["resize"] = "ok"
        buf = _io.BytesIO()
        sm.save(buf, format="PNG")
        chain["save_png"] = "%d 字节" % len(buf.getvalue())
    except Exception:
        chain["pil_err"] = _tb.format_exc()
    if buf is not None:
        try:
            import tkinter as _tk2
            r2 = _tk2.Tk()
            r2.withdraw()
            ph = _tk2.PhotoImage(data=_b64.b64encode(buf.getvalue()).decode("ascii"))
            chain["tk_data"] = "ok %dx%d" % (ph.width(), ph.height())
            r2.destroy()
        except Exception:
            chain["tk_data_err"] = _tb.format_exc()
    out["image_chain"] = chain

    path = os.path.join(tc.BASE_DIR, "diag.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = sys.argv[1:]

    if "--diag" in args:
        return _diag()

    if "--probe" in args:
        return _probe()

    if "--panel" in args:
        sys.argv = [sys.argv[0]] + _strip("--panel", args)
        import my_devices
        return my_devices.main()

    if "--tray" in args:
        sys.argv = [sys.argv[0]] + _strip("--tray", args)
        import tray_app
        return tray_app.main()

    if args:
        # --status / --quit / --autostart / --install / --once 之类，交给对应模块
        import tray_app
        return tray_app.main()

    # 不带参数：双击启动
    import tray_common as tc

    if not tc.is_tray_running():
        if not tc.start_tray_app(wait_timeout=20.0):
            return 1
    return 0 if tc.open_ui_window() else 1


if __name__ == "__main__":
    raise SystemExit(main())
