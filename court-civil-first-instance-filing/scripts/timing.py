#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量耗时埋点：每步一行 JSON，落在 `<案件目录>/06运行记录/timing.jsonl`。

为什么需要：以前只能靠“感觉慢”，没法回答到底哪一步吃时间。
现在每次运行都留下 `步骤 / 耗时 / 请求数`，优化前后可以直接对比。

用法：
    from timing import StepTimer
    timer = StepTimer(case_dir)
    with timer.step("upload"):
        ...
    timer.record("total", 123.4, requests=81)
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator


class StepTimer:
    def __init__(self, case_dir: Path, filename: str = "timing.jsonl") -> None:
        self.path = Path(case_dir) / "06运行记录" / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.marks: Dict[str, float] = {}

    def record(self, step: str, seconds: float, **extra: Any) -> None:
        line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "step": step,
                "seconds": round(float(seconds), 3), **extra}
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @contextmanager
    def step(self, name: str, **extra: Any) -> Iterator[None]:
        started = time.time()
        try:
            yield
        finally:
            self.record(name, time.time() - started, **extra)
