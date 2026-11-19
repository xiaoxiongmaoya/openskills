#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证码 OCR 环境自检（先修环境，修不好才退回人工）。

用法：
    python ocr_probe.py                       # 只验证 import + 引擎初始化
    python ocr_probe.py --image captcha.png   # 再跑一次真实识别

退出码：0 = 可用；2 = 依赖缺失；3 = DLL 初始化失败（版本不兼容）；4 = 识别异常。

背景（实测）：onnxruntime >= 1.22 在本机 Win10 19045 上
`onnxruntime.dll` 的 DllMain 直接失败（WinError 1114），降 1.20.1 才正常。
本脚本把这段判断固化下来，避免升级后又要靠试。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PINNED_ORT = "1.20.1"


def _hint(msg: str) -> None:
    print(f"[FIX] {msg}")


def check_imports() -> int:
    try:
        import numpy  # noqa: F401
        import onnxruntime as ort
    except ImportError as exc:
        print(f"[FAIL] 依赖缺失: {exc}")
        _hint(f'pip install ddddocr "onnxruntime=={PINNED_ORT}" coloredlogs sympy')
        return 2
    except OSError as exc:  # WinError 1114 走这里
        print(f"[FAIL] onnxruntime 加载失败: {exc}")
        _hint(f'降级：pip install --no-deps --force-reinstall "onnxruntime=={PINNED_ORT}"')
        _hint("装完补依赖：pip install coloredlogs sympy")
        return 3

    print(f"[OK] onnxruntime {ort.__version__}  providers={ort.get_available_providers()}")
    if ort.__version__ != PINNED_ORT:
        _hint(f"当前 {ort.__version__} 非钉死版本 {PINNED_ORT}；本机升级到 1.22+ 会挂，确认无风险再改")

    try:
        import ddddocr  # noqa: F401
    except ImportError as exc:
        print(f"[FAIL] ddddocr 缺失: {exc}")
        _hint("pip install ddddocr")
        return 2
    print(f"[OK] ddddocr {getattr(ddddocr, '__version__', 'unknown')}")
    return 0


def check_engine() -> int:
    try:
        import ddddocr

        engine = ddddocr.DdddOcr(show_ad=False)  # 触发模型加载，比 import 更能暴露问题
    except Exception as exc:  # pragma: no cover - 环境相关
        print(f"[FAIL] OCR 引擎初始化失败: {exc}")
        _hint("模型文件可能未随包安装，重装 ddddocr")
        return 4
    print("[OK] OCR 引擎初始化")
    return 0


def check_image(path: Path) -> int:
    import ddddocr

    if not path.exists():
        print(f"[FAIL] 图片不存在: {path}")
        return 4
    engine = ddddocr.DdddOcr(show_ad=False)
    try:
        code = engine.classification(path.read_bytes())
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL] 识别异常: {exc}")
        return 4
    print(f"[OK] 识别结果: {code!r}（{len(str(code))} 位）")
    if len(str(code)) != 4:
        print("[WARN] 法院验证码为 4 位，本次识别长度不符，环境可用但识别质量需关注")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="验证码 OCR 环境自检")
    ap.add_argument("--image", help="可选：真实验证码图片路径")
    args = ap.parse_args()

    print(f"python: {sys.version.split()[0]}  {sys.executable}")
    for step in (check_imports, check_engine):
        code = step()
        if code:
            return code
    if args.image:
        return check_image(Path(args.image))
    print("[DONE] 环境可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
