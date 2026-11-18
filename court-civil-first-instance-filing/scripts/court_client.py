#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法院民事一审在线立案通用客户端（登录 + 提交链路）。

能力：
- SM2 国密加密登录（gmssl，缺失时给出明确提示）
- 验证码 OCR（ddddocr + 4 变体投票 + 0↔O/1↔I 纠错），失败自动转人工输入
- 会话持久化（cookies + token + JWT exp 解析，过期前 3 分钟主动重登）
- 民事一审立案全链路：法院匹配 / 建草稿 / 模板 / 案由 / 当事人 / 代理人 /
  标的金额 / 材料上传（签名→OSS→关联）/ 诉请 / 最终提交
- 最终提交前的人工确认钩子（默认命令行确认，可注入自定义 confirm 函数）

凭证来源（按优先级）：构造函数参数 > 环境变量 COURT_USERNAME / COURT_PASSWORD。
本文件不含任何真实凭证，可安全分发。依赖：
    pip install requests gmssl pillow ddddocr
"""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import logging
import os
import random
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("缺少依赖：pip install requests")

LOG = logging.getLogger("court_client")

DEFAULT_BASE_URL = "https://zxfw.court.gov.cn"
DEFAULT_SESSION_FILE = ".court_session.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
LOGIN_PAGE_PATH = "/zxfw/index.html#/pagesGrxx/pc/login/index"
PREFIX_USER = "/yzw/yzw-zxfw-yhfw"
PREFIX_FILING = "/yzw/yzw-zxfw-lafw"
PREFIX_CASE = "/yzw/yzw-zxfw-ajfw"
TOKEN_EXPIRY_BUFFER_SECONDS = 180
CAPTCHA_EXPECTED_LENGTH = 4
CAPTCHA_MAX_ATTEMPTS = 3
REQUEST_RETRY_BACKOFF = (2.0, 4.0, 8.0)
DEFAULT_RATE = 1.0          # 请求/秒（账号级）；compliance.md 红线，别随手调高
CAUSE_CACHE_TTL_SECONDS = 7 * 24 * 3600

# ---- 案件类型（真实抓包：执行立案与民事一审共用 /api/v3/layy，只差三个字段） ---- #
# sp = 民事一审；zx = 首次执行（强制执行申请）
CASE_TYPES: Dict[str, Dict[str, Any]] = {
    "sp": {"ajcx": "sp", "ajlb": "sp", "ajlx": "sp", "lafs": "2"},
    "zx": {"ajcx": "zx", "ajlb": "zx", "ajlx": "1501_000001-1002", "lafs": None},
}

# 执行依据类别码表（抓自强制执行页"执行依据类别"下拉）
ZXYJ_LB: Dict[str, str] = {
    "民商": "1501_11400101-1",
    "刑事": "1501_11400101-2",
    "行政": "1501_11400101-3",
    "行政赔偿": "1501_11400101-4",
    "国内仲裁": "1501_11400101-5",
    "非诉行政": "1501_11400101-6",
    "先予执行": "1501_11400101-7",
    "司法罚款": "1501_11400101-8",
    "公证债权文书": "1501_11400101-9",
    "外国港澳台法院民事裁判": "1501_11400101-10",
    "外国港澳台仲裁裁决": "1501_11400101-11",
    "妨碍诉讼（执行）行为制裁": "1501_11400101-12",
    "其他": "1501_11400101-13",
}

# 诉讼地位编码：一审是 030109 族，执行是 100225 族（申请人码已实测，被申请人同族推定）
ROLE_CODES: Dict[str, Dict[str, str]] = {
    "sp": {"plaintiff": "1501_030109-1", "defendant": "1501_030109-2"},
    "zx": {"plaintiff": "1501_100225-1", "defendant": "1501_100225-2"},
}

# 材料槽位：执行立案与一审完全不同（实测草稿 sscls）
EXEC_SLOTS: Dict[str, str] = {
    "11800016-2": "执行申请书",
    "11800016-8": "执行依据文书",
    "11800016-1": "申请人身份材料",
    "11800016-9": "授权委托书及代理人身份证明",
    "11800016-254": "送达地址确认书",
    "11800016-36": "收款账户确认书",
}

# ---- 材料上传专用节流（法院风控最敏感的一段，宁可慢） ---------------------- #
# 单份材料之间的最小间隔（秒）。调小 = 更容易撞 OSS 403 / "获取上传信息失败"。
DEFAULT_UPLOAD_INTERVAL = 3.5
# 间隔上的随机抖动（秒）。固定节拍最像脚本，抖一下更接近人工点击。
DEFAULT_UPLOAD_JITTER = 1.5
# 每传 N 份材料做一次长停顿（份），<=0 表示不做批间停顿。
DEFAULT_UPLOAD_BATCH = 6
# 批间长停顿秒数。
DEFAULT_UPLOAD_COOLDOWN = 25.0
UPLOAD_STATE_NAME = ".court_upload.state"


# --------------------------------------------------------------------------- #
# 跨进程工具：文件锁 / 全局限速 / 共享会话路径
# --------------------------------------------------------------------------- #
if os.name == "nt":  # pragma: no cover - 平台相关
    import msvcrt

    def _lock_handle(handle) -> None:
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.02)

    def _unlock_handle(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:  # pragma: no cover
    import fcntl

    def _lock_handle(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_handle(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def workspace_root(case_dir: Path) -> Path:
    """案件目录 → 工作空间根。`<根>/案件库/<案件>` 取上两级；其它情况退化为案件目录本身。"""
    case_dir = Path(case_dir).resolve()
    if case_dir.parent.name == "案件库":
        return case_dir.parent.parent
    return case_dir


def session_file_for(case_dir: Path) -> Path:
    """统一会话文件：默认 `<工作空间根>/通用件/.court_session.json`，可被环境变量覆盖。

    token 有效期 1 小时，共用一份即可：多案只登录一次，不再每案一次验证码。
    """
    env = os.getenv("COURT_SESSION_FILE", "").strip()
    target = Path(env) if env else workspace_root(case_dir) / "通用件" / ".court_session.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def rate_state_file_for(case_dir: Path, filename: str = ".court_rate.json") -> Path:
    """跨进程限速状态文件（与 session 同目录）。"""
    return session_file_for(case_dir).with_name(filename)


def _env_float(name: str, default: float, override: Optional[float] = None) -> float:
    """取值优先级：显式参数 > 环境变量 > 默认值。环境变量写错就当没写。"""
    if override is not None:
        return float(override)
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            LOG.warning("环境变量 %s=%r 不是数字，改用默认 %s", name, raw, default)
    return float(default)


class GlobalRateLimiter:
    """跨进程全局限速：文件锁 + 时间戳，保证**所有 worker 合计**不超过 rate 次/秒。

    为什么不是每个进程自己 sleep：多案并发时各自 sleep 1.2s 等于把速率放大到 N 倍，
    那才是真正会招来风控的写法。
    """

    def __init__(self, interval: float, state_file: Optional[Path] = None) -> None:
        self.interval = max(0.0, float(interval))
        self.state_file = Path(state_file) if state_file else None

    def wait(self) -> None:
        if self.interval <= 0:
            return
        if self.state_file is None:
            time.sleep(self.interval)
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # 状态直接写在锁文件里（第 0 字节是锁位，其后是 JSON），避免两份文件之间的竞态
        handle = None
        for _ in range(200):
            try:
                handle = open(self.state_file, "r+b")
                break
            except FileNotFoundError:
                try:
                    handle = open(self.state_file, "w+b")
                    break
                except OSError:
                    time.sleep(0.02)
            except PermissionError:
                time.sleep(0.02)
        if handle is None:
            time.sleep(self.interval)
            return
        with handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_handle(handle)
            try:
                handle.seek(1)
                raw = handle.read()
                last = 0.0
                if raw.strip():
                    try:
                        last = float(json.loads(raw.decode("utf-8")).get("last") or 0)
                    except (ValueError, json.JSONDecodeError):
                        last = 0.0
                wait = last + self.interval - time.time()
                if wait > 0:
                    time.sleep(wait)
                payload = json.dumps({"last": time.time()}).encode("utf-8")
                handle.seek(1)
                handle.truncate()
                handle.write(payload)
                handle.flush()
            finally:
                _unlock_handle(handle)

class UploadPacer:
    """材料上传专用节流：跨进程互斥 + 固定间隔 + 随机抖动 + 批间长停顿。

    为什么不能只靠 GlobalRateLimiter：全局令牌桶管的是「所有接口请求平均 1 次/秒」，
    但两个 worker 各自上传时，请求依然会在同一秒内交错打到签名接口和 OSS，
    行为特征还是像机器 —— 这就是以前多线程上传容易吃 OSS 403 的原因。

    这里做两件事：
    1. **整段上传串行**：`slot()` 全程持有跨进程文件锁，同一时刻只有一个进程在上传
       （签名 → OSS 直传 → 关联槽位 三步都在锁内，中间不会插进别的上传）。
    2. **拉开节奏**：每次进锁先等 `interval + random(0, jitter)`，每 `batch` 份再长停 `cooldown`。

    失败也要占位（间隔照算），否则连续失败会变成高频重试，更容易被判定异常。
    """

    def __init__(self, interval: float = DEFAULT_UPLOAD_INTERVAL,
                 jitter: float = DEFAULT_UPLOAD_JITTER,
                 batch: int = DEFAULT_UPLOAD_BATCH,
                 cooldown: float = DEFAULT_UPLOAD_COOLDOWN,
                 state_file: Optional[Path] = None,
                 enabled: bool = True) -> None:
        self.interval = max(0.0, float(interval))
        self.jitter = max(0.0, float(jitter))
        self.batch = int(batch or 0)
        self.cooldown = max(0.0, float(cooldown))
        self.state_file = Path(state_file) if state_file else None
        self.enabled = bool(enabled) and (self.interval > 0 or self.jitter > 0 or self.batch > 0)

    def _pause(self) -> None:
        delay = self.interval + (random.uniform(0, self.jitter) if self.jitter else 0.0)
        if delay > 0:
            time.sleep(delay)

    @contextmanager
    def slot(self, label: str = "") -> Iterator[None]:
        if not self.enabled:
            yield
            return

        if self.state_file is None:
            # 没有共享状态文件时退化为进程内节流（单进程场景足够）
            self._pause()
            yield
            return

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        for _ in range(300):
            try:
                handle = open(self.state_file, "r+b")
                break
            except FileNotFoundError:
                try:
                    handle = open(self.state_file, "w+b")
                    break
                except OSError:
                    time.sleep(0.02)
            except PermissionError:
                time.sleep(0.02)
        if handle is None:                       # 拿不到状态文件就别卡死，纯等待
            self._pause()
            yield
            return

        last, count = 0.0, 0
        with handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_handle(handle)                 # ← 从这里开始，全机器只有这一个上传
            try:
                handle.seek(1)
                raw = handle.read()
                if raw.strip():
                    try:
                        state = json.loads(raw.decode("utf-8"))
                        last = float(state.get("last") or 0)
                        count = int(state.get("count") or 0)
                    except (ValueError, json.JSONDecodeError):
                        last, count = 0.0, 0

                wait = last + self.interval + (random.uniform(0, self.jitter) if self.jitter else 0.0) - time.time()
                if wait > 0:
                    LOG.info("上传节流：等 %.1fs%s", wait, f"（{label}）" if label else "")
                    time.sleep(wait)
                if self.batch > 0 and count > 0 and count % self.batch == 0:
                    LOG.info("已连续上传 %d 份，长停 %.0fs 降风控", count, self.cooldown)
                    if self.cooldown > 0:
                        time.sleep(self.cooldown)

                try:
                    yield
                finally:
                    handle.seek(1)
                    handle.truncate()
                    handle.write(json.dumps({"last": time.time(), "count": count + 1}).encode("utf-8"))
                    handle.flush()
            finally:
                _unlock_handle(handle)


FILING_STATUS_MAP = {
    "11800007-100": "草稿",
    "11800007-1": "待审核",
    "11800007-2": "审核通过",
    "11800007-3": "审核未通过",
    "11800007-4": "已立案",
    "11800007-6": "补充材料",
}

CAPTCHA_CORRECTION_RULES = (
    {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B",
     "o": "O", "i": "I", "z": "Z", "s": "S", "g": "G", "b": "B"},
    {"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8",
     "o": "0", "i": "1", "l": "1", "z": "2", "s": "5", "g": "6", "b": "8"},
)


class CourtApiError(Exception):
    """法院接口业务错误（message 已尽量转为可读中文）。"""

    def __init__(self, message: str, status_code: int = 400, payload: Any = None):
        super().__init__(message)
        self.message = str(message)
        self.status_code = status_code
        self.payload = payload


# --------------------------------------------------------------------------- #
# SM2 加密
# --------------------------------------------------------------------------- #
class SM2Encryptor:
    """基于 gmssl 的 SM2 加密器（与法院前端 SM2 组件参数保持一致）。"""

    def __init__(self, public_key_hex: str):
        try:
            from gmssl import sm2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("未安装 gmssl，请先执行 pip install gmssl") from exc

        key = (public_key_hex or "").strip()
        if key.startswith("04"):
            key = key[2:]
        if len(key) < 128:
            half = len(key) // 2
            key = key[:half].zfill(64) + key[half:].zfill(64)
        self._sm2 = sm2.CryptSM2(public_key=key, private_key="", asn1=False, mode=1)

    def encrypt(self, plaintext: str) -> str:
        return self._sm2.encrypt((plaintext or "").encode("utf-8")).hex()


# --------------------------------------------------------------------------- #
# 验证码 OCR
# --------------------------------------------------------------------------- #
class CaptchaRecognizer:
    """ddddocr 识别 + 4 种预处理变体投票 + 易混字符双向纠错。"""

    VARIANTS = ("base", "enhanced", "sharpen", "invert")

    def __init__(self) -> None:
        self._engine = None
        self._init_error = ""

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        if self._init_error:
            return None
        try:
            import ddddocr  # type: ignore
        except ImportError as exc:
            self._init_error = "未安装 ddddocr（pip install ddddocr），验证码转人工识别"
            LOG.warning(self._init_error)
            return None
        try:
            self._engine = ddddocr.DdddOcr(show_ad=False)
        except Exception as exc:  # pragma: no cover - 引擎初始化失败
            self._init_error = f"OCR 引擎初始化失败: {exc}"
            return None
        return self._engine

    def recognize(self, captcha_base64: str) -> Dict[str, Any]:
        raw = (captcha_base64 or "").strip()
        if not raw:
            return {"code": "", "success": False, "message": "验证码图片为空"}
        try:
            from PIL import Image, ImageFilter, ImageOps
        except ImportError:
            return {"code": "", "success": False, "message": "未安装 Pillow，验证码转人工识别"}

        if "," in raw and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            image = Image.open(io.BytesIO(base64.b64decode(raw)))
        except Exception as exc:
            return {"code": "", "success": False, "message": f"验证码图片解码失败: {exc}"}

        engine = self._get_engine()
        if engine is None:
            return {"code": "", "success": False, "message": self._init_error or "OCR 引擎未就绪"}

        results: List[Dict[str, Any]] = []
        for variant in self.VARIANTS:
            try:
                results.append(self._run_variant(engine, image, variant, Image, ImageFilter, ImageOps))
            except Exception as exc:  # 单变体失败不阻塞其它变体
                LOG.debug("captcha variant %s failed: %s", variant, exc)
                results.append({"code": "", "confidence": None})

        voted = self._vote(results)
        if voted is not None:
            return voted

        best = max(results, key=lambda item: item.get("confidence") or 0.0)
        return {
            "code": best.get("code") or "",
            "success": False,
            "message": "OCR 识别结果不可靠，请人工输入验证码",
        }

    def _run_variant(self, engine, image, variant: str, Image, ImageFilter, ImageOps) -> Dict[str, Any]:
        candidate = image
        if variant == "enhanced":
            candidate = ImageOps.autocontrast(image)
        elif variant == "sharpen":
            candidate = image.filter(ImageFilter.SHARPEN)
        elif variant == "invert":
            candidate = ImageOps.invert(image.convert("RGB")) if image.mode != "RGBA" else image

        buffer = io.BytesIO()
        candidate.save(buffer, format="PNG")
        outcome = engine.classification(buffer.getvalue())
        if isinstance(outcome, tuple):
            code, confidence = outcome[0], outcome[1] if len(outcome) > 1 else None
        elif isinstance(outcome, dict):
            code, confidence = outcome.get("code") or outcome.get("result") or "", outcome.get("confidence")
        else:
            code, confidence = str(outcome or ""), None
        return {"code": self._correct(str(code or "").strip()), "confidence": confidence}

    @staticmethod
    def _correct(code: str) -> str:
        if not code:
            return ""
        digits = sum(ch.isdigit() for ch in code)
        letters = sum(ch.isalpha() for ch in code)
        if digits == letters:
            return code
        table = CAPTCHA_CORRECTION_RULES[0] if digits > letters else CAPTCHA_CORRECTION_RULES[1]
        return "".join(table.get(ch, ch) for ch in code)

    @staticmethod
    def _vote(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        valid = [item for item in results if item.get("code") and len(item["code"]) == CAPTCHA_EXPECTED_LENGTH]
        if not valid:
            return None
        counter: Dict[str, int] = {}
        for item in valid:
            counter[item["code"]] = counter.get(item["code"], 0) + 1
        best_code, best_count = max(counter.items(), key=lambda kv: kv[1])
        if best_count >= 2:
            return {"code": best_code, "success": True, "message": f"OCR 投票通过（{best_count}/{len(valid)}）"}
        if len(counter) == 1:
            return {"code": best_code, "success": True, "message": "OCR 单一结果"}
        return None


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class CourtClient:
    """法院诉讼服务平台通用客户端（登录 + 民事一审立案链路）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        username: Optional[str] = None,
        password: Optional[str] = None,
        user_type: str = "lsyh",
        session_file: str = DEFAULT_SESSION_FILE,
        timeout: int = 25,
        polite_delay: float = 1.0,
        confirm_hook: Optional[Callable[[str], bool]] = None,
        interactive: bool = True,
        rate: Optional[float] = None,
        rate_state_file: Optional[str] = None,
        use_remote_cache: bool = True,
        upload_interval: Optional[float] = None,
        upload_jitter: Optional[float] = None,
        upload_batch: Optional[int] = None,
        upload_cooldown: Optional[float] = None,
        serialize_upload: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or os.getenv("COURT_USERNAME", "")
        self.password = password or os.getenv("COURT_PASSWORD", "")
        self.user_type = user_type
        self.interactive = interactive
        self._last_captcha_id = ""
        self.session_file = Path(session_file)
        self.timeout = timeout
        self.polite_delay = polite_delay
        # rate 给定时走跨进程令牌桶（推荐）；否则退回每进程 polite_delay（旧行为）
        self.limiter = GlobalRateLimiter(1.0 / rate, rate_state_file) if rate else None
        # 上传单独一条慢通道：串行 + 间隔 + 抖动 + 批间长停（见 UploadPacer 注释）
        upload_state = (Path(rate_state_file).with_name(UPLOAD_STATE_NAME) if rate_state_file
                        else self.session_file.with_name(UPLOAD_STATE_NAME))
        self.upload_pacer = UploadPacer(
            interval=_env_float("COURT_UPLOAD_INTERVAL", DEFAULT_UPLOAD_INTERVAL, upload_interval),
            jitter=_env_float("COURT_UPLOAD_JITTER", DEFAULT_UPLOAD_JITTER, upload_jitter),
            batch=int(_env_float("COURT_UPLOAD_BATCH", DEFAULT_UPLOAD_BATCH, upload_batch)),
            cooldown=_env_float("COURT_UPLOAD_COOLDOWN", DEFAULT_UPLOAD_COOLDOWN, upload_cooldown),
            state_file=upload_state,
            enabled=serialize_upload,
        )
        self.confirm_hook = confirm_hook or self._default_confirm
        self.recognizer = CaptchaRecognizer()
        self._session = requests.Session()
        # 单次运行内的远端状态缓存：detail / 槽位文件列表 / 当事人 / 排队数 / 提交配置
        self._cache_enabled = use_remote_cache
        self._cache: Dict[str, Dict[str, Any]] = {
            "detail": {}, "slots": {}, "parties": {}, "queue": {}, "config": {}
        }
        self._cause_tree_memory: Dict[str, List[Dict[str, Any]]] = {}
        self.cause_cache_file: Optional[Path] = None
        self._relogin_in_progress = False
        self.request_count = 0
        self._session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.base_url,
            "Referer": urljoin(self.base_url + "/", LOGIN_PAGE_PATH.lstrip("/")),
        })
        self._public_key = ""
        self._load_session()

    # ---------------- 基础 ---------------- #
    @property
    def api_base(self) -> str:
        return f"{self.base_url}{PREFIX_USER}"

    def _polite(self) -> None:
        if self.limiter is not None:
            self.limiter.wait()
            return
        if self.polite_delay > 0:
            time.sleep(self.polite_delay)

    def set_cache_dir(self, directory: Path) -> None:
        """案由树等磁盘缓存目录（建议 `<案件目录>/05案件信息/cache`）。"""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.cause_cache_file = directory / "ay_tree_0300.json"

    def _cached(self, bucket: str, key: str) -> Any:
        if not self._cache_enabled:
            return None
        return self._cache[bucket].get(key)

    def _remember(self, bucket: str, key: str, value: Any) -> Any:
        if self._cache_enabled:
            self._cache[bucket][key] = value
        return value

    @staticmethod
    def _extract_message(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            for key in ("message", "msg", "detail", "error"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return fallback

    @staticmethod
    def _looks_like_success(payload: dict) -> bool:
        if payload.get("success") is True:
            return True
        if payload.get("code") == 200:
            return True
        data = payload.get("data")
        if isinstance(data, dict) and any(data.get(key) for key in ("token", "userId", "loginName")):
            return True
        return False

    def request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        absolute: bool = False,
        allow_relogin: bool = True,
    ) -> Dict[str, Any]:
        """统一 JSON 请求；业务 code 非 200 时抛 CourtApiError。

        401/403 时自动重登一次（凭证存在的前提下），避免会话过期把整条链打断。
        """
        url = path if absolute else urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Accept": "application/json, text/plain, */*"}
        kwargs: Dict[str, Any] = {"timeout": self.timeout, "headers": headers}
        if method.lower() == "get":
            kwargs["params"] = payload or {}
        else:
            kwargs["json"] = payload or {}
            headers["Content-Type"] = "application/json;charset=UTF-8"

        last_error: Exception = RuntimeError("未知错误")
        for attempt, backoff in enumerate((*REQUEST_RETRY_BACKOFF, 0.0), start=1):
            self._polite()
            self.request_count += 1
            try:
                response = self._session.request(method.upper(), url, **kwargs)
                if response.status_code in (401, 403):
                    # 只清本进程内存，不删共享会话文件（多 worker 共用一份）
                    self._clear_session(purge_file=False)
                    if allow_relogin and not self._relogin_in_progress and self.username and self.password:
                        LOG.info("法院会话已失效 → 自动重新登录一次")
                        self._relogin_in_progress = True
                        try:
                            self.login()
                        finally:
                            self._relogin_in_progress = False
                        return self.request_json(method, path, payload, absolute=absolute,
                                                 allow_relogin=False)
                    raise CourtApiError("法院会话已失效，请重新登录", status_code=401)
                response.raise_for_status()
                data = response.json() if response.content else {}
                break
            except CourtApiError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                LOG.warning("请求失败（第 %d 次）: %s", attempt, exc)
                if backoff <= 0:
                    raise CourtApiError(f"法院接口请求失败: {exc}", status_code=500) from exc
                time.sleep(backoff)
        else:  # pragma: no cover
            raise CourtApiError(f"法院接口请求失败: {last_error}", status_code=500)

        if not isinstance(data, dict):
            raise CourtApiError("法院接口返回结构异常", status_code=500)
        if not self._looks_like_success(data):
            message = self._extract_message(data, "法院接口调用失败")
            code = str(data.get("errorCode") or "").strip()
            if code:
                # 法院的 message 经常是泛化文案（如"提交网上立案信息失败"），
                # 把 errorCode 带出来，排查时能直接对上服务端日志。
                message = f"{message}（errorCode={code}）"
            raise CourtApiError(message, status_code=400, payload=data)
        return data

    # ---------------- 会话持久化 ---------------- #
    def _save_session(self, token: str, expires_at: Optional[float] = None) -> None:
        state = {
            "base_url": self.base_url,
            "cookies": requests.utils.dict_from_cookiejar(self._session.cookies),
            "token": token,
            "expires_at": expires_at,
            "saved_at": int(time.time()),
        }
        try:
            # 原子写：多进程共用一份 session 时，避免别的 worker 读到半截 JSON
            tmp = self.session_file.with_name(self.session_file.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self.session_file)
            os.chmod(self.session_file, 0o600)
        except OSError as exc:
            LOG.warning("会话持久化失败（不影响本次运行）: %s", exc)

    def _load_session(self) -> None:
        if not self.session_file.exists():
            return
        try:
            state = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if state.get("base_url") and state["base_url"] != self.base_url:
            return
        self._session.cookies.update({str(k): str(v) for k, v in (state.get("cookies") or {}).items() if v})
        token = state.get("token") or ""
        if token:
            self._session.headers["Authorization"] = token
            self._session.headers["authorization"] = token
        self._token_expires_at = state.get("expires_at")

    def _clear_session(self, purge_file: bool = False) -> None:
        """清空本进程会话。`purge_file=True` 才会删除磁盘文件。

        默认不删：多 worker 共用 `<根>/通用件/.court_session.json` 时，
        一个 worker 的 401 不应该把其他人一起登出。
        """
        self._session.cookies.clear()
        self._session.headers.pop("Authorization", None)
        self._session.headers.pop("authorization", None)
        self._token_expires_at = None
        if not purge_file:
            return
        try:
            self.session_file.unlink()
        except OSError:
            pass

    @staticmethod
    def _decode_jwt_exp(token: str) -> Optional[float]:
        if not token or token.count(".") < 2:
            return None
        try:
            segment = token.split(".")[1]
            padding = "=" * (-len(segment) % 4)
            payload = json.loads(base64.urlsafe_b64decode((segment + padding).encode("utf-8")))
        except Exception:
            return None
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
        if isinstance(exp, str) and exp.isdigit():
            return float(exp)
        return None

    def _session_valid(self) -> bool:
        token = self._session.headers.get("Authorization", "")
        if not token:
            return False
        exp = getattr(self, "_token_expires_at", None) or self._decode_jwt_exp(token)
        if exp and time.time() >= exp - TOKEN_EXPIRY_BUFFER_SECONDS:
            return False
        return True

    # ---------------- 登录 ---------------- #
    def fetch_public_key(self) -> str:
        payload = self.request_json("get", f"{PREFIX_USER}/api/v1/publicKey")
        key = str((payload.get("data") or "")).strip()
        if not key:
            raise CourtApiError("获取法院公钥失败")
        self._public_key = key
        return key

    def fetch_captcha(self) -> Dict[str, str]:
        payload = self.request_json("get", f"{PREFIX_USER}/api/v1/captcha")
        data = payload.get("data") or {}
        captcha_id = str(data.get("id") or "").strip()
        image = str(data.get("jpg") or data.get("img") or "").strip()
        if not captcha_id or not image:
            raise CourtApiError("获取法院验证码失败")
        self._last_captcha_id = captcha_id
        return {"id": captcha_id, "image": image}

    def dump_captcha(self, path: str) -> Dict[str, str]:
        """取验证码并落盘（人工/视觉识别流程用），返回 captcha id 与图片路径。"""
        captcha = self.fetch_captcha()
        raw = captcha["image"].split(",", 1)[-1]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(raw))
        return {"id": captcha["id"], "image": str(target)}

    def login_with_captcha(self, captcha_id: str, code: str) -> Dict[str, Any]:
        """用已获取的 captcha_id + 人工识别码登录（不重新取验证码，避免 id 与图片错配）。"""
        if not self.username or not self.password:
            raise CourtApiError("缺少法院账号或密码")
        encrypted = SM2Encryptor(self.fetch_public_key()).encrypt(self.password)
        body = {"login": self.username, "pass": encrypted, "yzm": code,
                "id": captcha_id, "type": self.user_type}
        payload = self.request_json("post", f"{PREFIX_USER}/api/v1/login", body)
        token = self._extract_token(payload)
        if token:
            self._session.headers["Authorization"] = token
            self._session.headers["authorization"] = token
        self._save_session(token, self._decode_jwt_exp(token))
        return {"success": True, "message": "法院登录成功", "token_expires_at": self._decode_jwt_exp(token)}

    def recognize_captcha(self, image_base64: str) -> str:
        result = self.recognizer.recognize(image_base64)
        if result.get("success") and result.get("code"):
            LOG.info("验证码 OCR 成功: %s", result.get("message"))
            return str(result["code"])
        if not self.interactive:
            # 非交互环境（脚本/Agent）：落盘图片后抛错，由外部视觉识别后走 login_with_captcha
            raw = (image_base64 or "").split(",", 1)[-1]
            target = Path("captcha.png")
            try:
                target.write_bytes(base64.b64decode(raw))
            except Exception:  # pragma: no cover
                pass
            raise CourtApiError(
                f"验证码 OCR 失败：{result.get('message') or '识别不可靠'}；"
                f"图片已存 {target.resolve()}，请视觉识别后调用 login_with_captcha(captcha_id, code)"
            )
        # OCR 不可用/不可靠 → 人工输入兜底
        print(f"[提示] {result.get('message') or '验证码需要人工识别'}")
        code = input("请输入验证码: ").strip()
        if len(code) != CAPTCHA_EXPECTED_LENGTH:
            raise CourtApiError(f"验证码长度应为 {CAPTCHA_EXPECTED_LENGTH} 位")
        return code

    def login(self, captcha_code: Optional[str] = None, max_attempts: int = CAPTCHA_MAX_ATTEMPTS) -> Dict[str, Any]:
        if not self.username or not self.password:
            raise CourtApiError("缺少法院账号或密码（参数或环境变量 COURT_USERNAME/COURT_PASSWORD）")

        for attempt in range(1, max_attempts + 1):
            public_key = self.fetch_public_key()
            if captcha_code and attempt == 1 and self._last_captcha_id:
                captcha_id, code = self._last_captcha_id, captcha_code
                captcha = {"id": captcha_id, "image": ""}
            else:
                captcha = self.fetch_captcha()
                code = self.recognize_captcha(captcha["image"])
            encrypted = SM2Encryptor(public_key).encrypt(self.password)
            body = {
                "login": self.username,
                "pass": encrypted,
                "yzm": code,
                "id": captcha["id"],
                "type": self.user_type,
            }
            try:
                payload = self.request_json("post", f"{PREFIX_USER}/api/v1/login", body)
            except CourtApiError as exc:
                message = exc.message
                if "密码" in message:
                    raise CourtApiError(f"法院密码错误：{message}。请修改密码后重试。", status_code=400) from exc
                if attempt >= max_attempts:
                    raise CourtApiError(f"法院登录失败：{message}", status_code=400) from exc
                LOG.info("验证码错误，第 %d/%d 次重试", attempt, max_attempts)
                captcha_code = None
                continue

            token = self._extract_token(payload)
            if token:
                self._session.headers["Authorization"] = token
                self._session.headers["authorization"] = token
            self._save_session(token, self._decode_jwt_exp(token))
            return {"success": True, "message": "法院登录成功", "token_expires_at": self._decode_jwt_exp(token)}
        raise CourtApiError("法院登录失败：超过最大尝试次数", status_code=400)

    @staticmethod
    def _extract_token(payload: dict) -> str:
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("token", "Authorization", "authorization"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("token", "Authorization", "authorization"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def ensure_login(self) -> None:
        if self._session_valid():
            return
        if self._session.cookies or self._session.headers.get("Authorization"):
            self._clear_session()
        self.login()

    def status(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "username_masked": (self.username[:2] + "****") if self.username else "",
            "logged_in": self._session_valid(),
            "session_file": str(self.session_file),
        }

    # ---------------- 用户信息 / 码表 / 案由 ----------------
    def get_user_info(self) -> Dict[str, Any]:
        """登录用户信息。律师用户返回 cLszyzh（执业证号）、cName、cZjhm、cPhone。"""
        payload = self.request_json("get", "/yzw/yzw-zxfw-sxfw/api/v1/user/info")
        return payload.get("data") or {}

    def get_code_table(self, code_type: str) -> List[Dict[str, Any]]:
        """码表查询。常用：000011 当事人类型 / 000013 代理人类型 / 030109 诉讼地位 /
        GB0001 性别 / GB0006 国别 / 000015 证件类型。"""
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v1/cyzl/code", {"codeType": code_type})
        return payload.get("data") or []

    def get_cause_tree(self, lbs: str = "0300", *, refresh: bool = False) -> List[Dict[str, Any]]:
        """案由树（lbs=0300 民事案由）。注意：必须 GET + lbs，POST 会返回 405。

        一次运行里 `find_cause` 会被调多次，这里做内存 + 磁盘（TTL 7 天）双层缓存，
        避免为了同一个案由反复拉整棵树。
        """
        if not refresh and lbs in self._cause_tree_memory:
            return self._cause_tree_memory[lbs]
        if not refresh and self.cause_cache_file and self.cause_cache_file.exists():
            try:
                blob = json.loads(self.cause_cache_file.read_text(encoding="utf-8"))
                if (time.time() - float(blob.get("saved_at") or 0)) < CAUSE_CACHE_TTL_SECONDS:
                    tree = blob.get("tree") or []
                    if tree:
                        self._cause_tree_memory[lbs] = tree
                        return tree
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v1/ay/tree/batch", {"lbs": lbs})
        data: Any = payload.get("data") or {}
        if isinstance(data, dict):
            data = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(data, dict):
            data = data.get(lbs) or []
        tree = data if isinstance(data, list) else []
        self._cause_tree_memory[lbs] = tree
        if self.cause_cache_file:
            try:
                self.cause_cache_file.write_text(
                    json.dumps({"saved_at": time.time(), "lbs": lbs, "tree": tree}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                LOG.debug("案由树缓存写入失败: %s", exc)
        return tree

    def find_cause(self, name: str, lbs: str = "0300") -> Optional[Dict[str, Any]]:
        """按名称定位案由节点。返回 {'id','fjm','name','path'}。

        重要：法院 layy 对象里 `laay` 存的是案由**名称**，`laayMz` 存的是案由树节点 **id**，
        两者不要写反（写反会导致前端直接显示编码串）。
        """
        target = (name or "").strip()
        if not target:
            return None
        hit: List[Tuple[str, Dict[str, Any]]] = []

        def walk(nodes: Any, path: List[str]) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_name = str(node.get("name") or "")
                trail = path + [node_name]
                if node_name == target or str(node.get("fjm") or "") == target:
                    hit.append(("/".join(trail), node))
                walk(node.get("children"), trail)

        walk(self.get_cause_tree(lbs), [])
        if not hit:
            return None
        path_text, node = hit[0]
        return {"id": str(node.get("id") or ""), "fjm": str(node.get("fjm") or ""),
                "name": str(node.get("name") or ""), "path": path_text}

    def save_cause(self, filing_id: str, cause_name: str, lbs: str = "0300") -> Dict[str, Any]:
        node = self.find_cause(cause_name, lbs)
        if not node:
            raise CourtApiError(f"案由树中未找到“{cause_name}”，请人工确认名称")
        return self.patch_layy({"id": filing_id, "laay": node["name"], "laayMz": node["id"]})

    # ---------------- 法院与案由 ---------------- #
    def search_courts(self, region_code: str, keyword: str = "") -> List[Dict[str, Any]]:
        payload = self.request_json(
            "get", f"{PREFIX_FILING}/api/v3/pz/fy", {"sfid": region_code, "city": "", "fymc": keyword}
        )
        return self._find_records(payload)

    def get_civil_entry(self, region_code: str) -> Dict[str, Any]:
        """从区域立案类型树中找“民事一审”入口。"""
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v3/pz/fy/ajlx/{region_code}")
        entry = self._find_entry_by_name(payload, "民事一审")
        if not entry:
            raise CourtApiError("当前区域未识别到民事一审入口，请人工确认平台与区域代码")
        return entry

    # ---------------- 立案草稿 ---------------- #
    def create_filing(self, fyid: str, region_code: str = "", profile: Optional[dict] = None,
                      case_type: str = "sp") -> Dict[str, Any]:
        """建立案草稿。`case_type`: sp=民事一审 / zx=强制执行申请。

        实测：两类案件共用 `POST /api/v3/layy`，仅 `ajcx`/`ajlb`/`ajlx` 不同，
        执行案件不传 `lafs`。其余字段与一审一致。
        """
        ct = CASE_TYPES.get(case_type) or CASE_TYPES["sp"]
        profile = profile or {}
        ajlx = str(profile.get("ajlx") or "")
        if not ajlx:
            if case_type == "zx":
                ajlx = ct["ajlx"]
            else:
                entry = self.get_civil_entry(region_code) if region_code else {}
                ajlx = str(entry.get("ajlx") or "")
        payload = {
            "ajcx": ct["ajcx"],
            "ajlx": ajlx,
            "tjcgsqsfqr": "1",
            "fyid": fyid,
            "sqrsf": str(profile.get("sqrsf") or "11800010-1"),
            "ajlb": ct["ajlb"],
            "pcSqrLx": str(profile.get("pcSqrLx") or ""),
            "sqrlx": str(profile.get("sqrlx") or "11800011-2"),
            "sfid": region_code,
            "citymc": "",
            "ftmc": "",
            "sfzscq": "1501_000010-2",
            "sfysla": "1501_000010-2",
        }
        if ct.get("lafs"):
            payload["lafs"] = ct["lafs"]
        return self.request_json("post", f"{PREFIX_FILING}/api/v3/layy", payload)

    # ---------------- 执行立案专属（真实抓包） ---------------- #
    def save_zxyj(self, layy_id: str, fyid: str, zxyj: dict) -> Dict[str, Any]:
        """写「执行依据 + 执行标的」：`PATCH /api/v3/zxyj`。

        `zxyj` 需含：`lb`(类别名，如"民商")、`ah`(执行依据文书文号)、
        `jbjg`(作出单位代码，数字)、`jbjgMc`(单位名称)；`ysaj*` 可选（来自原审案号候选）。

        执行标的与执行请求同在这个记录上（实测前端 submit 逻辑）：
          - `bd_zl`: 标的种类编码列表，如 ["1501_100279-1"]（金钱给付）
          - `bd_je`: 金钱给付金额
          - `zxqq`: 执行请求正文
          - `zxyjzw`: 执行理由正文（对应字段名是 zxyjzw，不是 zxly）
        """
        lb = str(zxyj.get("lb") or "民商")
        code = ZXYJ_LB.get(lb)
        if not code:
            raise CourtApiError(f"未知执行依据类别「{lb}」，可选：{'、'.join(ZXYJ_LB)}")
        bd_zl = zxyj.get("bd_zl") or zxyj.get("sqzxbdzl") or ""
        if isinstance(bd_zl, (list, tuple)):
            bd_zl = ",".join(str(x) for x in bd_zl)
        payload = {
            "jbjg": zxyj.get("jbjg"),
            "jbjgMc": str(zxyj.get("jbjgMc") or zxyj.get("jbjg_mc") or ""),
            "zxyjAh": str(zxyj.get("ah") or ""),
            "zxyjlb": code,
            "zxyjmc": lb,
            "layyId": layy_id,
            "fyId": fyid,
            "sqzxbdzl": str(bd_zl),
            "sqzxbdje": str(zxyj.get("bd_je") or zxyj.get("sqzxbdje") or ""),
            "zxqq": str(zxyj.get("zxqq") or ""),
            "zxyjzw": str(zxyj.get("zxyjzw") or zxyj.get("zxly") or ""),
        }
        if zxyj.get("id"):
            payload["id"] = zxyj["id"]
        return self.request_json("patch", f"{PREFIX_FILING}/api/v3/zxyj", payload)

    def save_ysxx(self, layy_id: str, fyid: str, ysxx: dict) -> Dict[str, Any]:
        """写「原审信息」：`PATCH /api/v3/layy/ysxx`。

        `ysxx` 需含：`ysfyid`(原审法院代码)、`ysfymc`、`ysajbh`(原审案件 id)、
        `ysajAjbs`(案件标识)、`ysajah`(原审案号)。通常由 `list_ysaj()` 选中项直接给出。
        """
        payload = {
            "layyId": layy_id,
            "fyId": fyid,
            "ysfyid": ysxx.get("ysfyid"),
            "ysajbh": str(ysxx.get("ysajbh") or ""),
            "ysfymc": str(ysxx.get("ysfymc") or ""),
            "ysajAjbs": str(ysxx.get("ysajAjbs") or ""),
            "ysajah": str(ysxx.get("ysajah") or ""),
        }
        return self.request_json("patch", f"{PREFIX_FILING}/api/v3/layy/ysxx", payload)

    def list_ysaj(self) -> List[Dict[str, Any]]:
        """原审案号候选（已结案件）：`POST /yzw/yzw-zxfw-ajfw/api/v1/ysajlist`。

        返回项关键字段：`cah` 案号、`cfydm` 法院代码、`cajbh` 案件 id、`cajbs` 案件标识。
        """
        payload = {"sfsx": 1, "ajlb": "1501_000001-0300;1501_000001-1300;", "sfid": "0"}
        data = self.request_json("post", f"{PREFIX_CASE}/api/v1/ysajlist", payload)
        return self._find_records(data)

    def get_template(self, fyid: str, ajlx: str) -> Dict[str, Any]:
        return self.request_json("get", f"{PREFIX_FILING}/api/v3/pz/layymb/{fyid}/{ajlx}")

    def patch_filing(self, payload: dict) -> Dict[str, Any]:
        """原始 PATCH。注意：`PATCH /api/v3/layy` 是**全量覆盖**，未带上的字段会被清空，
        业务写入请优先用 `patch_layy()`。"""
        return self.request_json("patch", f"{PREFIX_FILING}/api/v3/layy", payload)

    def patch_layy(self, updates: dict) -> Dict[str, Any]:
        """安全写入 layy：先拉远端现状做底，再合并 updates，最后整对象 PATCH。

        规避全量覆盖导致的 `laayMz`/`ajmc`/`sqbdje` 互相清空。
        PATCH 成功后把合并结果写回本地 detail 缓存，不再为了下一次写入重拉详情。
        """
        filing_id = str((updates or {}).get("id") or "")
        if not filing_id:
            raise CourtApiError("patch_layy 需要 layy.id")
        detail = self.get_filing_detail(filing_id)
        base = self._extract_layy(detail) or {}
        merged = dict(base)
        merged.update({k: v for k, v in updates.items() if v is not None})
        merged["id"] = filing_id
        result = self.patch_filing(merged)
        self._apply_layy_patch(detail, merged)
        return result

    @staticmethod
    def _apply_layy_patch(detail: dict, merged: dict) -> None:
        """把已提交的字段就地合并进缓存的 detail（避免下一处回读）。"""
        target_id = str(merged.get("id") or "")

        def walk(value: Any) -> bool:
            if isinstance(value, dict):
                if str(value.get("id") or "") == target_id and "laay" in value:
                    value.update(merged)
                    return True
                for child in value.values():
                    if walk(child):
                        return True
            elif isinstance(value, list):
                for child in value:
                    if walk(child):
                        return True
            return False

        walk(detail.get("data") if isinstance(detail, dict) else detail)

    def get_filing_detail(self, filing_id: str, *, refresh: bool = False) -> Dict[str, Any]:
        """立案单详情。单次运行内缓存同一份数据（写操作后就地合并，不再重读）。"""
        key = str(filing_id)
        if not refresh:
            cached = self._cached("detail", key)
            if cached is not None:
                return cached
        data = self.request_json("get", f"{PREFIX_FILING}/api/v3/layy/layyxq/{filing_id}/0")
        return self._remember("detail", key, data)

    def invalidate_detail(self, filing_id: str) -> None:
        self._cache["detail"].pop(str(filing_id), None)

    # ---------------- 当事人 / 代理人 ---------------- #
    def list_parties(self, filing_id: str, ajlx: str) -> List[Dict[str, Any]]:
        key = f"{filing_id}|{ajlx}"
        cached = self._cached("parties", key)
        if cached is not None:
            return cached
        records = self._find_records(
            self.request_json("get", f"{PREFIX_FILING}/api/v3/layy/dsr/list/{filing_id}/{ajlx}")
        )
        return self._remember("parties", key, records)

    def delete_party(self, filing_id: str, party_id: str) -> Dict[str, Any]:
        result = self.request_json("delete", f"{PREFIX_FILING}/api/v3/layy/dsr/{filing_id}/{party_id}")
        self._drop_parties_cache(filing_id)
        return result

    def save_party(self, payload: dict) -> Dict[str, Any]:
        result = self.request_json("post", f"{PREFIX_FILING}/api/v3/layy/dsr", payload)
        self._drop_parties_cache(str(payload.get("layyid") or ""))
        return result

    def _drop_parties_cache(self, filing_id: str) -> None:
        for key in [k for k in self._cache["parties"] if k.startswith(f"{filing_id}|")]:
            self._cache["parties"].pop(key, None)

    def save_agent(self, payload: dict, method: str = "post") -> Dict[str, Any]:
        return self.request_json(method, f"{PREFIX_FILING}/api/v3/layy/dlr", payload)

    def delete_agent(self, filing_id: str, agent_id: str) -> Dict[str, Any]:
        """删除代理人。**有代理人关联时法院不允许删当事人**（报「存在代理人关联关系」），
        所以重跑填字段要先删代理人再删当事人。"""
        return self.request_json("delete", f"{PREFIX_FILING}/api/v3/layy/dlr/{filing_id}/{agent_id}")

    def invalidate_parties(self, filing_id: str) -> None:
        self._drop_parties_cache(str(filing_id))

    # ---------------- 诉请 / 材料 / 提交 ---------------- #
    def save_claims(self, payload: dict) -> Dict[str, Any]:
        return self.request_json("post", f"{PREFIX_FILING}/api/v3/layy/ssqq", payload)

    def request_upload_signature(self, fyid: str, ext: str = ".pdf", cllx: str = "") -> Dict[str, Any]:
        payload = {"path": "layy", "ext": ext, "fydm": fyid, "cllx": cllx}
        result = self.request_json("post", f"{PREFIX_CASE}/api/v1/file/upload/signature", payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return data

    def upload_oss_file(self, oss_path: str, form_data: dict, file_path: str, file_name: str,
                        mime_type: str = "application/octet-stream") -> bool:
        with open(file_path, "rb") as handle:
            files = {"file": (file_name, handle.read(), mime_type or "application/octet-stream")}
            self._polite()
            self.request_count += 1
            response = self._session.post(
                oss_path, data=form_data, files=files, timeout=self.timeout, headers={"Accept": "*/*"}
            )
            response.raise_for_status()
        return True

    def save_material_attachment(self, payload: dict) -> Dict[str, Any]:
        return self.request_json("post", f"{PREFIX_FILING}/api/v3/layy/ssclfj", payload)

    def list_slot_files(self, filing_id: str, slot_id: str, *, refresh: bool = False) -> List[Dict[str, Any]]:
        """某材料槽位下的文件列表。接口只能按槽位单查，所以按槽位缓存 + 上传后增量追加。"""
        key = f"{filing_id}|{slot_id}"
        if not refresh:
            cached = self._cached("slots", key)
            if cached is not None:
                return cached
        listed = self.request_json(
            "get", f"{PREFIX_FILING}/api/v3/layy/ssclfj/list/{filing_id}/{slot_id}"
        )
        return self._remember("slots", key, listed.get("data") or [])

    def remember_slot_file(self, filing_id: str, slot_id: str, name: str, path: str = "") -> None:
        """上传成功后把文件记进本地槽位缓存，省掉后续 precheck 的回读。"""
        bucket = self._cache["slots"].get(f"{filing_id}|{slot_id}")
        if bucket is None:
            return
        bucket.append({"wjmc": name, "path": path})

    def upload_material(self, filing_id: str, fyid: str, slot: Dict[str, Any],
                        file_path: str, file_name: str = "", mime_type: str = "") -> Dict[str, Any]:
        """材料上传三步：取签名 → 直传 OSS → 关联槽位。slot 需含法院材料编码与槽位 ID。"""
        source = Path(file_path)
        if not source.exists():
            raise CourtApiError(f"材料文件不存在: {file_path}")
        file_name = file_name or source.name
        ext = source.suffix.lower() or ".pdf"
        cllx = str(slot.get("court_material_code") or "").strip()
        slot_id = str(slot.get("court_material_id") or slot.get("template_slot_id") or "").strip()
        if not cllx or not slot_id:
            raise CourtApiError("材料槽位缺少法院材料类型编码或模板 ID，请先读取法院模板")

        # 签名 → OSS 直传 → 关联槽位 三步必须在同一个节流区间内完成，
        # 否则多进程时各自交错，风控照样能识别出来（这是以前 OSS 403 的主因）。
        return self._do_upload(filing_id, fyid, slot, source, file_name, ext, cllx, slot_id, mime_type)

    def _do_upload(self, filing_id: str, fyid: str, slot: Dict[str, Any], source: Path,
                   file_name: str, ext: str, cllx: str, slot_id: str,
                   mime_type: str) -> Dict[str, Any]:
        with self.upload_pacer.slot(file_name):
            signature = self.request_upload_signature(fyid, ext=ext, cllx=cllx)
            oss_path = str(signature.get("ossPath") or "").strip()
            store_as = str(signature.get("storeAs") or "").strip()
            if not oss_path or not store_as:
                raise CourtApiError("未获取到材料上传地址")
            form_data = {
                "key": store_as,
                "policy": str(signature.get("policy") or ""),
                "OSSAccessKeyId": str(signature.get("ossaccessKeyId") or signature.get("OSSAccessKeyId") or ""),
                "success_action_status": "200",
                "signature": str(signature.get("signature") or ""),
                "x-oss-security-token": str(signature.get("token") or ""),
            }
            self.upload_oss_file(oss_path, form_data, str(source), file_name, mime_type)
            attachment = {
                "wjbh": store_as,
                "layyid": filing_id,
                "fyId": fyid,
                "wjmc": file_name,
                "path": store_as,
                "ssclid": slot_id,
                "cllx": cllx,
                "clmc": str(slot.get("court_material_name") or slot.get("slot_name") or ""),
                "bccl": 1,
                "name": file_name,
                "extname": ext.lstrip("."),
                "url": str(signature.get("url") or signature.get("previewUrl") or store_as),
                "xh": int(slot.get("court_material_order") or 1),
            }
            result = self.save_material_attachment(attachment)
            self.remember_slot_file(filing_id, slot_id, file_name, store_as)
            return result

    def get_submit_config(self, fyid: str) -> Dict[str, Any]:
        """提交配置：`tsy` 调解告知正文、`title` 弹窗标题、`sfkt` 是否走先行调解流程。"""
        key = str(fyid)
        cached = self._cached("config", key)
        if cached is not None:
            return cached
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v3/pz/tjpz", {"fyid": fyid})
        return self._remember("config", key, payload)

    def get_court_queue(self, fyid: str) -> int:
        """法院当前排期在前的案件数（`ajpqtj`）。前台弹窗用它拼“在您的案件之前有 N 件案件正在排期等待开庭审理”。"""
        key = str(fyid)
        cached = self._cached("queue", key)
        if cached is not None:
            return int(cached)
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v3/layy/sjzt/ajpqtj/count/{fyid}")
        value = payload.get("data")
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        self._remember("queue", key, number)
        return number

    def get_mediation_notice(self, fyid: str) -> Dict[str, Any]:
        """提交前必须给用户看的先行调解告知（原文取 tjpz，不要自己编）。"""
        config = self.get_submit_config(fyid)
        data = config.get("data") or {}
        queue = self.get_court_queue(fyid)
        return {
            "need_mediation": bool(data.get("sfkt")),
            "title": str(data.get("title") or ""),
            "text": str(data.get("tsy") or ""),
            "queue": queue,
            "prompt": (
                f"您好，您将向法院提交立案申请，在您的案件之前有 {queue} 件案件正在排期等待开庭审理。"
                "请问您是否考虑选择先行调解？"
            ),
        }

    @staticmethod
    def mediation_code(agree: bool) -> str:
        """调解意愿码：1501_000010-1 同意 / -2 不同意。

        注意：该字段**无法用独立 PATCH 写入**（返回 200 但不落库），只能在 submit 的
        完整 layy 对象里带上，由提交流程落库。
        """
        return "1501_000010-1" if agree else "1501_000010-2"

    @staticmethod
    def build_dsr_mc(parties: List[Dict[str, Any]], roles: Optional[Dict[str, str]] = None) -> str:
        """拼 `layy.dsrMc`（参与人展示串）。法院前台格式：`原告：A，B；被告：C`。

        只传当事人记录不改 dsrMc 时，列表/参与人栏缺“原告/被告”角色前缀（实测踩坑）。
        执行案件传 `roles=ROLE_CODES["zx"]`，标签变“申请执行人/被执行人”。
        """
        if roles:
            labels = (("申请执行人", roles["plaintiff"]), ("被执行人", roles["defendant"]))
        else:
            labels = (("原告", "1501_030109-1"), ("被告", "1501_030109-2"))
        code_to_label = {code: (label, idx) for idx, (label, code) in enumerate(labels)}
        buckets: Dict[int, List[str]] = {0: [], 1: []}
        for party in parties:
            slot = code_to_label.get(str(party.get("ssdw") or ""))
            if not slot:
                continue
            name = party.get("xm") or party.get("dwmc") or ""
            if name:
                buckets[slot[1]].append(str(name))
        parts = []
        for idx, (label, _code) in enumerate(labels):
            if buckets[idx]:
                parts.append(label + "：" + "，".join(buckets[idx]))
        return "；".join(parts)

    def sync_dsr_mc(self, filing_id: str, ajlx: str = "", case_type: str = "sp",
                    cause: str = "", ajmc: str = "") -> Dict[str, Any]:
        """按当前当事人重算 dsrMc / ajmc 并写回 layy。

        **必须 refresh**：刚 save/delete 完当事人时，缓存里的 dsrs 还是旧的
        （法院自动塞的“律师本人=原告”那条），用缓存会写出“原告：张三”。

        执行案件（case_type="zx"）用 100225 族诉讼地位码，案名按
        `{申请执行人}申请执行{被执行人}{原审案由}一案` 拼（法院页面显示“名称暂无”时
        就是 ajmc 为空，提交必报 `RMFY109905`，实测踩坑）。
        """
        roles = ROLE_CODES.get(case_type) or ROLE_CODES["sp"]
        detail = self.get_filing_detail(filing_id, refresh=True).get("data") or {}
        layy = detail.get("layy") or {}
        parties = detail.get("dsrs") or []
        mc = self.build_dsr_mc(parties, roles=roles)
        updates: Dict[str, Any] = {"id": filing_id}
        if mc:
            updates["dsrMc"] = mc
        plaintiff = next((p for p in parties if p.get("ssdw") == roles["plaintiff"]), None)
        defendant = next((p for p in parties if p.get("ssdw") == roles["defendant"]), None)
        sqr = str((plaintiff or {}).get("xm") or (plaintiff or {}).get("dwmc") or "")
        bsqr = str((defendant or {}).get("xm") or (defendant or {}).get("dwmc") or "")
        if case_type == "zx":
            explicit = str(ajmc or "").strip()
            if explicit:
                updates["ajmc"] = explicit          # spec 显式给案名 → 强制覆盖
            elif sqr and bsqr and not str(layy.get("ajmc") or "").strip():
                suffix = str(cause or "").strip()   # 已生成过就不动，避免冲掉人工修改
                updates["ajmc"] = f"{sqr}申请执行{bsqr}{suffix}一案"
        else:
            cause_name = str(layy.get("laay") or "")
            if plaintiff and defendant and cause_name:
                updates["ajmc"] = f"{sqr}诉{bsqr}{cause_name}一案"
        return self.patch_layy(updates)

    def precheck(self, filing_id: str, decisions: Optional[Dict[str, str]] = None,
                 *, refresh: bool = False, case_type: str = "sp") -> List[str]:
        """提交前硬性自查，返回问题列表（空列表 = 可提交）。

        `decisions` 承载两项**必须由用户拍板**的选择，缺一即中止：
          - `mediation`: yes / no（是否同意先行调解）
          - `defendant_id_photo`: upload / skip（是否上传被告身份证照片；身份信息照填，照片单独确认）

        执行案件（`case_type="zx"`）不适用先行调解，改查执行依据与执行请求。

        其中 `laay` 必须是案由**名称**；写成数字编码会让法院前台直接显示编码串（已实测踩坑）。
        读取全部走缓存：一次运行里反复调用不会重复请求法院。
        """
        decisions = decisions or {}
        problems: List[str] = []
        detail = self.get_filing_detail(filing_id, refresh=refresh)
        data = detail.get("data") or {}
        layy = self._extract_layy(detail) or {}
        if not layy.get("fyid"):
            problems.append("未确认受理法院")

        roles = ROLE_CODES.get(case_type) or ROLE_CODES["sp"]
        if case_type == "zx":
            zx_records = data.get("zxyjs") or []
            zx = zx_records[0] if zx_records else {}
            if not str(zx.get("zxyjAh") or layy.get("zxyjAh") or layy.get("ah") or "").strip():
                problems.append("未填写执行依据（执行依据文书文号为空）")
            # 执行请求/理由在 zxyjs[0].zxqq / .zxyjzw，不在 layy.ssqq/ssly
            if not str(zx.get("zxqq") or "").strip():
                problems.append("执行请求为空（应写入 zxyjs[0].zxqq）")
            if not str(zx.get("zxyjzw") or "").strip():
                problems.append("执行理由为空（应写入 zxyjs[0].zxyjzw）")
            if not str(zx.get("sqzxbdzl") or layy.get("sqzxbdzl") or "").strip():
                problems.append("执行标的类型为空（sqzxbdzl），提交会被拒")
            elif "1501_100279-1" in str(zx.get("sqzxbdzl") or layy.get("sqzxbdzl")) and not str(
                    zx.get("sqzxbdje") or "").strip():
                problems.append("执行标的类型含「金钱给付」，但执行标的金额（sqzxbdje）为空")
            dsrs = data.get("dsrs") or []
            if not any(d.get("ssdw") == roles["plaintiff"] for d in dsrs):
                problems.append("缺少申请执行人")
            if not any(d.get("ssdw") == roles["defendant"] for d in dsrs):
                problems.append("缺少被执行人")
            if dsrs and not str(layy.get("dsrMc") or "").strip():
                problems.append("layy.dsrMc 为空，先调 sync_dsr_mc()")
            # ajmc 为空 = 列表显示「名称暂无」，提交必报 RMFY109905（实测）
            if not str(layy.get("ajmc") or "").strip():
                problems.append("案名为空（ajmc）：法院列表显示「名称暂无」，提交必报 RMFY109905，"
                                "先调 sync_dsr_mc(case_type='zx', cause=原审案由)")
            # 执行申请书/执行依据文书：法院标"非必传"，但缺了必被退
            for code in ("11800016-2", "11800016-8"):
                slot = next((s for s in (data.get("sscls") or []) if s.get("cllx") == code), None)
                if slot and not self.list_slot_files(filing_id, slot["id"], refresh=refresh):
                    problems.append(f"材料未上传：{EXEC_SLOTS.get(code, code)}")
            return problems

        laay = str(layy.get("laay") or "").strip()
        if not laay:
            problems.append("案由为空")
        elif laay.isdigit() or laay.startswith("0020"):
            problems.append(f"laay 疑似填成了案由编码（{laay}）：laay 应填名称，节点 id 应放 laayMz")
        if not str(layy.get("ssqq") or "").strip():
            problems.append("诉讼请求为空")
        if not str(layy.get("ssly") or "").strip():
            problems.append("事实与理由为空")
        dsrs = data.get("dsrs") or []
        if not any(d.get("ssdw") == roles["plaintiff"] for d in dsrs):
            problems.append("缺少原告")
        if not any(d.get("ssdw") == roles["defendant"] for d in dsrs):
            problems.append("缺少被告")
        mc = str(layy.get("dsrMc") or "")
        if dsrs and ("原告：" not in mc or "被告：" not in mc):
            problems.append("layy.dsrMc 缺少“原告：/被告：”角色前缀（参与人栏会不显示原被告），先调 sync_dsr_mc()")
        mediation = (decisions.get("mediation") or "").strip().lower()
        if mediation not in ("yes", "no"):
            problems.append(
                "未确定「是否同意先行调解」：先用 get_mediation_notice(fyid) 把排期案件数与原话给用户，"
                "用户选后把 decisions['mediation'] 传进来（yes/no）"
            )
        elif str(layy.get("lasfzsqt") or "") not in ("", self.mediation_code(mediation == "yes")):
            problems.append(
                f"草稿里的调解意愿（lasfzsqt={layy.get('lasfzsqt')}）与用户选择不一致，"
                "提交时会按用户选择覆盖"
            )

        id_photo = (decisions.get("defendant_id_photo") or "").strip().lower()
        if id_photo not in ("upload", "skip"):
            problems.append(
                "未确定「是否上传被告身份证照片」：身份信息（证件号）可以照填，但照片必须由用户单独确认，"
                "确认后把 decisions['defendant_id_photo'] 传 upload 或 skip"
            )
        elif id_photo == "upload":
            slot_id = next((s["id"] for s in (data.get("sscls") or [])
                            if s.get("cllx") == "11800016-1"), "")
            files: List[Dict[str, Any]] = []
            if slot_id:
                files = self.list_slot_files(filing_id, slot_id, refresh=refresh)
            if not any("被告" in str(f.get("wjmc") or "") for f in files):
                problems.append("已选择上传被告身份证照片，但「当事人身份证明」槽位没有被告证件文件")

        # 代理律师必须上传律师执业证（法院实务硬要求，模板里却是"非必传"）
        if data.get("dlrs"):
            agent_slot = next((s for s in (data.get("sscls") or [])
                               if s.get("cllx") == "11800016-9"), None)
            agent_files: List[Dict[str, Any]] = []
            if agent_slot:
                agent_files = self.list_slot_files(filing_id, agent_slot["id"], refresh=refresh)
            names = [str(f.get("wjmc") or "") for f in agent_files]
            if not any(("律师证" in n or "执业证" in n) for n in names):
                problems.append("代理律师未上传律师证：请在「委托代理人委托手续和身份材料」槽补律师执业证")
        for slot in (data.get("sscls") or []):
            code = str(slot.get("cllx") or "")
            if code in ("11800016-2", "11800016-4", "11800016-254") and not (
                self.list_slot_files(filing_id, slot["id"], refresh=refresh)
            ):
                problems.append(f"必传材料未上传：{slot.get('clmc')}")
        return problems

    def submit(self, filing_id: str, decisions: Optional[Dict[str, str]] = None,
               *, skip_precheck: bool = False, case_type: str = "sp") -> Dict[str, Any]:
        """最终提交：预检（含两项用户决策）→ 拉草稿详情组装完整 layy 对象 → 经人工确认后 PATCH submit。

        `skip_precheck=True` 用于调用方刚刚跑过 precheck / confirm_snapshot 的场景，避免同一份数据读三遍。
        执行案件（`case_type="zx"`）没有先行调解，不写 `lasfzsqt`。
        """
        if not skip_precheck:
            problems = self.precheck(filing_id, decisions, case_type=case_type)
            if problems:
                raise CourtApiError("提交前自查未通过：" + "；".join(problems))
        detail = self.get_filing_detail(filing_id)
        layy = self._extract_layy(detail)
        if not layy:
            raise CourtApiError("未能从立案详情提取 layy 对象，请检查草稿是否有效")
        layy.setdefault("id", filing_id)
        if case_type == "zx":
            # 实测：提交体带上执行标的（zxyjs[0] 里的 sqzxbdzl/sqzxbdje）才稳，
            # layy 自身不回写这两个字段，必须显式搬运。
            zx = ((detail.get("data") or {}).get("zxyjs") or [{}])[0]
            for key in ("sqzxbdzl", "sqzxbdje"):
                if zx.get(key) and not layy.get(key):
                    layy[key] = zx.get(key)
        mediation = (decisions or {}).get("mediation", "").strip().lower()
        if case_type == "sp" and mediation in ("yes", "no"):
            # 调解意愿只能随提交对象一起落库，不能单独 PATCH
            layy["lasfzsqt"] = self.mediation_code(mediation == "yes")
        summary = json.dumps(
            {key: layy.get(key) for key in ("id", "fyid", "ajlx", "laay", "laayMz", "sqbdje") if layy.get(key)},
            ensure_ascii=False,
        )
        if not self.confirm_hook(f"即将向法院提交立案单 {filing_id}，关键字段: {summary}。确认提交？"):
            raise CourtApiError("用户取消提交", status_code=400)
        return self.request_json("patch", f"{PREFIX_FILING}/api/v3/layy/submit", layy)

    def withdraw_layy(self, filing_id: str) -> Dict[str, Any]:
        """撤回已提交的立案申请：`POST /api/v3/layy/chlayy/{id}`，成功后 zt 回草稿（11800007-100）。

        法院侧有冷却期：刚提交的一段时间内返回「该立案暂不允许撤回，请稍后尝试」，
        这不是接口错，等一会儿再试。
        """
        return self.request_json("post", f"{PREFIX_FILING}/api/v3/layy/chlayy/{filing_id}", {})

    def delete_filing(self, filing_id: str) -> Dict[str, Any]:
        """彻底删除草稿：`DELETE /api/v3/layy/{id}`（实测 200）。

        只对草稿有效；已提交的单先 `withdraw_layy` 退回草稿再删。
        抓包/联调留下的测试草稿用它清掉，别让账号里堆垃圾。
        """
        return self.request_json("delete", f"{PREFIX_FILING}/api/v3/layy/{filing_id}")

    def diagnose_submit_error(self, filing_id: str, message: str) -> str:
        if "起诉状不能为空" not in (message or ""):
            return message
        try:
            self.request_json("post", f"{PREFIX_FILING}/api/v3/layy/ssclfj/actions/zzqsz", {"layyid": filing_id})
        except CourtApiError as exc:
            if "请选择签名文件" in exc.message:
                return "法院仍未把当前诉状识别为有效起诉状材料，请检查材料槽位映射、诉状上传记录与法院草稿是否一致"
        return message

    # ---------------- 列表与状态 ---------------- #
    @staticmethod
    def parse_review_records(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把 `data.shjgs`（法院审核结果列表）转成中文记录，按审核时间倒序。

        字段实测（前端「审核结果」区块同源）：
        `cshjg` 审核结果、`shyj` **审核意见正文**、`shsj` 审核时间、`shrxm` 审核人、
        `cbhyy` 驳回原因、`thfjwjs` 退回附件（带签名 URL）、`bcclSj` 材料补正时间。

        ⚠️ 审核意见**只存在这里**：`layy.shyj` 法院端从不写值（12 案实测全 null）。
        以前只读 `layy.shyj` 所以永远拿不到驳回理由。
        """
        records: List[Dict[str, Any]] = []
        for raw in (data.get("shjgs") or []):
            if not isinstance(raw, dict):
                continue
            files = []
            for item in (raw.get("thfjwjs") or []):
                if isinstance(item, dict):
                    name = str(item.get("wjmc") or item.get("name") or "").strip()
                    url = str(item.get("url") or "").strip()
                    if name or url:
                        files.append({"name": name, "url": url})
            code = str(raw.get("shjg") or "")
            records.append({
                "result": str(raw.get("cshjg") or "").strip() or FILING_STATUS_MAP.get(code, code),
                "status_code": code,
                "opinion": str(raw.get("shyj") or "").strip(),
                "reviewed_at": raw.get("shsj") or "",
                "reviewer": str(raw.get("shrxm") or ""),
                "reject_reason": str(raw.get("cbhyy") or "").strip(),
                "reject_code": str(raw.get("bhyy") or ""),
                "mediation": str(raw.get("sftj") or ""),
                "material_fix_at": raw.get("bcclSj") or "",
                "returned_files": [f for f in files if f["url"]],
                "id": raw.get("id") or "",
            })
        records.sort(key=lambda r: str(r.get("reviewed_at") or ""), reverse=True)
        return records

    def download_attachment(self, url: str, dest: Path) -> Path:
        """按签名 URL 下载法院附件（退回的材料 / 法院给的模板）。

        **不走 session**：OSS 收到 Authorization 头会当签名字段校验，直接 403。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._polite()
        self.request_count += 1
        response = requests.get(url, timeout=max(self.timeout, 60), headers={"Accept": "*/*"})
        response.raise_for_status()
        dest.write_bytes(response.content)
        return dest

    def get_filing_status(self, filing_id: str, include_materials: bool = False) -> Dict[str, Any]:
        """按立案单 id 查状态——后续核对审核进度用这个，一次拿全。

        只依赖 `layy/layyxq/{id}/0`，不依赖列表接口，单号可直接查。
        审核意见取 `data.shjgs` 最新一条（`layy.shyj` 法院端恒为空）。
        """
        detail = self.get_filing_detail(filing_id)
        data = detail.get("data") or {}
        layy = self._extract_layy(detail) or {}
        reviews = self.parse_review_records(data)
        latest = reviews[0] if reviews else {}
        status_code = str(layy.get("zt") or "")
        parties = [
            {
                "name": p.get("xm") or p.get("dwmc"),
                "role": {"1501_030109-1": "原告", "1501_030109-2": "被告",
                         "1501_030109-5": "第三人"}.get(str(p.get("ssdw") or ""), str(p.get("ssdw") or "")),
                "id_card": p.get("zjhm") or p.get("zzhm"),
            }
            for p in (data.get("dsrs") or [])
        ]
        agents = [
            {"name": a.get("xm"), "license": a.get("zyzh"), "firm": a.get("zyjg"),
             "for_party": a.get("bdlrMc")}
            for a in (data.get("dlrs") or [])
        ]
        result: Dict[str, Any] = {
            "id": layy.get("id") or filing_id,
            "court": layy.get("fymc"),
            "fyid": layy.get("fyid"),
            "cause": layy.get("laay"),
            "cause_id": layy.get("laayMz"),
            "amount": layy.get("sqbdje"),
            "status": status_code,
            "status_text": FILING_STATUS_MAP.get(status_code, status_code),
            "submitted_at": layy.get("tjsj"),
            "case_no": layy.get("ah") or layy.get("spajah"),
            "review_opinion": latest.get("opinion") or layy.get("shyj") or "",
            "review_result": latest.get("result") or "",
            "review_at": latest.get("reviewed_at") or "",
            "reviewer": latest.get("reviewer") or "",
            "reject_reason": latest.get("reject_reason") or "",
            "returned_files": latest.get("returned_files") or [],
            "material_fix_at": latest.get("material_fix_at") or "",
            "review_records": reviews,
            "mediation": layy.get("lasfzsqt"),
            "parties": parties,
            "agents": agents,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if include_materials:
            materials: Dict[str, List[str]] = {}
            for slot in (data.get("sscls") or []):
                listed = self.request_json(
                    "get", f"{PREFIX_FILING}/api/v3/layy/ssclfj/list/{filing_id}/{slot['id']}"
                )
                materials[str(slot.get("clmc") or "")] = [
                    str(f.get("wjmc") or "") for f in (listed.get("data") or [])
                ]
            result["materials"] = materials
        return result

    @staticmethod
    def get_origin(layy: Dict[str, Any]) -> Dict[str, Any]:
        """判断立案单来源：`layyIdy` 非空 = 从旧单复制而来（复制/继续提交路径）。"""
        copied_from = str((layy or {}).get("layyIdy") or "").strip()
        return {
            "kind": "copy" if copied_from else "new",
            "copied_from": copied_from,
            "label": "复制草稿提交" if copied_from else "新建草稿提交",
            "mediation_persisted": "unknown" if copied_from else "expected",
        }

    def confirm_snapshot(self, filing_id: str, decisions: Optional[Dict[str, str]] = None,
                         *, case_type: str = "sp") -> Dict[str, Any]:
        """**给用户确认用的快照：全部字段回读自法院，不用本地入参。**

        每条都带 ok 判定，写错的地方会直接标出来（例如案由被写成编码、dsrMc 缺角色前缀）。
        提交前的确认必须贴这个结果，不能贴本地 spec。
        执行案件（`case_type="zx"`）改看执行依据、执行请求，材料清单换成执行槽位。
        """
        is_zx = case_type == "zx"
        detail = self.get_filing_detail(filing_id, refresh=True)
        data = detail.get("data") or {}
        layy = self._extract_layy(detail) or {}
        status = self.get_filing_status(filing_id)

        cause_name = str(layy.get("laay") or "")
        cause_id = str(layy.get("laayMz") or "")
        cause_display_ok = bool(cause_name) and not cause_name.isdigit() and not cause_name.startswith("0020")
        cause_id_ok = cause_id.isdigit()

        mc = str(layy.get("dsrMc") or "")
        if is_zx:
            dsr_mc_ok = ("申请执行人" in mc) and ("被执行人" in mc)
        else:
            dsr_mc_ok = ("原告：" in mc) and ("被告：" in mc)

        if is_zx:
            zx_records = data.get("zxyjs") or []
            zxyj_ah = str((zx_records[0].get("zxyjAh") if zx_records else "") or layy.get("zxyjAh") or "")
            required = {code: name for code, name in EXEC_SLOTS.items() if code in ("11800016-2", "11800016-8")}
        else:
            zxyj_ah = ""
            required = {"11800016-2": "起诉状", "11800016-1": "当事人身份证明",
                        "11800016-4": "证据目录及证据材料", "11800016-254": "送达地址确认书"}
        materials: List[Dict[str, Any]] = []
        for slot in (data.get("sscls") or []):
            files = self.list_slot_files(filing_id, slot["id"], refresh=True)
            names = [str(f.get("wjmc") or "") for f in files]
            code = str(slot.get("cllx") or "")
            must = code in required
            materials.append({
                "slot": slot.get("clmc"), "cllx": code, "files": names,
                "required": must, "ok": bool(names) if must else True,
            })

        all_files = [n for m in materials for n in m["files"]]
        photo_uploaded = any("被告" in n or "被执行" in n for n in all_files)
        photo_decision = (decisions or {}).get("defendant_id_photo") or (
            "upload" if photo_uploaded else "skip")
        photo_ok = (photo_decision == "upload") == photo_uploaded if not is_zx else True

        mediation_code = str(layy.get("lasfzsqt") or "")
        mediation_text = {"1501_000010-1": "同意", "1501_000010-2": "不同意"}.get(mediation_code, "法院端未记录")
        chosen = (decisions or {}).get("mediation", "").strip().lower()
        if not mediation_code and chosen in ("yes", "no"):
            mediation_text = ("用户选同意" if chosen == "yes" else "用户选不同意") + "（法院端不记录该字段）"
        origin = self.get_origin(layy)
        mediation_note = "" if mediation_code else (
            "lasfzsqt 无法通过 API 写入（PATCH 与 submit 均实测不落库），调解意愿需在法院前台选择；"
            "本地 case.json 已记录用户决定"
            + (f"；本单为{origin['label']}" if origin["kind"] == "copy" else "")
        )

        effective: Dict[str, str] = dict(decisions or {})
        effective.setdefault("defendant_id_photo", photo_decision)
        if mediation_code == "1501_000010-1":
            effective.setdefault("mediation", "yes")
        elif mediation_code == "1501_000010-2":
            effective.setdefault("mediation", "no")

        snapshot = {
            "id": layy.get("id") or filing_id,
            "origin": origin,
            "case_type": case_type,
            "court": layy.get("fymc"), "fyid": layy.get("fyid"),
            "cause": ({"zxyjAh": zxyj_ah,
                       "display_ok": bool(zxyj_ah),
                       "laay": cause_name, "laayMz": cause_id}
                      if is_zx else
                      {"laay": cause_name, "laayMz": cause_id,
                       "display_ok": cause_display_ok, "id_ok": cause_id_ok,
                       "fixed": None if cause_display_ok else (
                           self.find_cause(cause_id) or self.find_cause(cause_name))}),
            "amount": layy.get("sqbdje"),
            "ajmc": layy.get("ajmc"),
            "dsrMc": {"value": mc, "ok": dsr_mc_ok},
            "parties": status["parties"],
            "agents": status["agents"],
            "claims": ({"ssqq_len": len((data.get("zxyjs") or [{}])[0].get("zxqq") or "") if data.get("zxyjs") else 0,
                        "ssly_len": len((data.get("zxyjs") or [{}])[0].get("zxyjzw") or "") if data.get("zxyjs") else 0,
                        "bd_zl": (data.get("zxyjs") or [{}])[0].get("sqzxbdzl") if data.get("zxyjs") else "",
                        "bd_je": (data.get("zxyjs") or [{}])[0].get("sqzxbdje") if data.get("zxyjs") else ""}
                       if is_zx else
                       {"ssqq_len": len(layy.get("ssqq") or ""), "ssly_len": len(layy.get("ssly") or "")}),
            "materials": materials,
            # 执行案件：无先行调解，材料里也没有被执行人身份证照片槽 —— 两项闸门都不适用
            "gates": ({"mediation": {"value": "不适用", "code": "", "ok": True,
                                     "note": "执行案件无先行调解"},
                       "defendant_id_photo": {"value": "不适用", "uploaded": photo_uploaded,
                                              "ok": True}} if is_zx else {
                "mediation": {"value": mediation_text, "code": mediation_code,
                              "ok": bool(mediation_code) or chosen in ("yes", "no"),
                              "note": mediation_note},
                "defendant_id_photo": {"value": photo_decision, "uploaded": photo_uploaded,
                                       "ok": photo_ok},
            }),
            "status": status,
            "problems": self.precheck(filing_id, effective, case_type=case_type),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "_source": "court",  # 强调：全部回读自法院，不是本地 spec
        }
        return snapshot

    def list_layy(self, page: int = 1, limit: int = 20) -> List[Dict[str, Any]]:
        """立案单列表（含草稿与已提交）。参数名是 page + limit，用 pageSize/size 会报 400。

        返回记录里 `laay` 是案由名称、`laayMz` 是案由节点 id —— 可用来核对写入是否正确。
        """
        payload = self.request_json("get", f"{PREFIX_FILING}/api/v3/layy", {"page": page, "limit": limit})
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def list_filings(self, page: int = 1, page_size: int = 10) -> List[Dict[str, Any]]:
        """案件列表（立案通过后才会出现在这里；刚提交的立案单用 list_layy 查）。"""
        payload = {
            "pageNum": page,
            "pageSize": page_size,
            "ajlb": "sp",
            "cxtj": "",
            "kssj": "",
            "jssj": "",
            "zt": "",
            "sfid": "",
            "sqrsf": "",
        }
        return self._find_records(self.request_json("post", f"{PREFIX_CASE}/api/v1/ajlist", payload))

    # ---------------- 工具 ---------------- #
    @staticmethod
    def _find_records(payload: Any) -> List[Dict[str, Any]]:
        """从任意层级响应中提取记录列表（兼容 data/data.list/records 等结构）。"""
        found: List[Dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                if value and all(isinstance(item, dict) for item in value):
                    found.extend(value)
                else:
                    for item in value:
                        walk(item)
            elif isinstance(value, dict):
                for key in ("list", "records", "rows", "content", "data", "result"):
                    if key in value:
                        walk(value[key])

        walk(payload)
        return found

    @staticmethod
    def _find_entry_by_name(payload: Any, target: str) -> Optional[Dict[str, Any]]:
        """在立案类型树中定位名称匹配 target 且带 ajlx 编码的入口节点。"""

        def walk(value: Any) -> Optional[Dict[str, Any]]:
            if isinstance(value, dict):
                names = [str(value.get(key) or "") for key in ("mc", "name", "ajlxmc", "ajlxMc", "label", "title")]
                if any(target == name or target in name for name in names) and value.get("ajlx"):
                    return value
                for item in value.values():
                    found = walk(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = walk(item)
                    if found:
                        return found
            return None

        return walk(payload)

    @staticmethod
    def _extract_layy(detail_payload: Any) -> Optional[Dict[str, Any]]:
        def walk(value: Any) -> Optional[Dict[str, Any]]:
            if isinstance(value, dict):
                if "ajlx" in value and ("fyid" in value or "id" in value):
                    return value
                for item in value.values():
                    found = walk(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = walk(item)
                    if found:
                        return found
            return None

        return walk(detail_payload)

    @staticmethod
    def _default_confirm(prompt: str) -> bool:
        print(f"\n[人工确认] {prompt}")
        answer = input("输入 yes 确认，其它任意键取消: ").strip().lower()
        return answer in ("yes", "y")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_client(args: argparse.Namespace) -> CourtClient:
    password = args.password or os.getenv("COURT_PASSWORD") or ""
    if not password and sys.stdin.isatty():
        password = getpass.getpass("法院密码: ")
    return CourtClient(
        base_url=args.base_url,
        username=args.username or os.getenv("COURT_USERNAME") or "",
        password=password,
        user_type=args.user_type,
        session_file=args.session_file,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="法院民事一审在线立案通用客户端")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="法院平台 base_url")
    parser.add_argument("--username", default="", help="法院账号（或环境变量 COURT_USERNAME）")
    parser.add_argument("--password", default="", help="法院密码（或环境变量 COURT_PASSWORD）")
    parser.add_argument("--user-type", default="lsyh", help="用户类型，默认 lsyh（律师用户）")
    parser.add_argument("--session-file", default=DEFAULT_SESSION_FILE, help="会话持久化文件路径")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="登录法院平台（OCR 失败自动转人工输入）")
    sub.add_parser("status", help="查看当前会话状态")

    filing = sub.add_parser("filing", help="查询民事一审入口 / 法院 / 立案列表（只读）")
    filing.add_argument("--region", default="330000", help="省级区域代码，默认 330000")
    filing.add_argument("--fyid", default="", help="法院 ID（提供时查询该院提交配置）")
    filing.add_argument("--keyword", default="", help="法院名称关键词")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = _build_client(args)

    if args.command == "login":
        result = client.login()
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "status":
        print(json.dumps(client.status(), ensure_ascii=False))
        return 0

    if args.command == "filing":
        client.ensure_login()
        entry = client.get_civil_entry(args.region)
        print(f"民事一审入口（region={args.region}）: {entry.get('name')}")
        courts = client.search_courts(args.region, args.keyword)
        for court in courts[:20]:
            print(f"  法院: {court.get('fymc') or court.get('mc')} | fyid={court.get('fyid') or court.get('id')}")
        if args.fyid:
            config = client.get_submit_config(args.fyid)
            print(f"提交配置（fyid={args.fyid}）: {json.dumps(config.get('data', config), ensure_ascii=False)[:500]}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
