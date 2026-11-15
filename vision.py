# -*- coding: utf-8 -*-
"""DeepSeek 识图：截图 -> 网格坐标 / 方位角。

实测结论（别随手改）：
- 模型 id 是 deepseek-flash（DeepSeek-V4.1-Flash，支持图片输入），必须先吐 reasoning_content，
  max_tokens 给小了 content 会是空的 -> 用 900，并且带重试。
- 罗盘读数（0–359 的细刻度）比网格坐标难得多：同一张图有时能直接答，有时会一路推理到被截断。
  所以方位角走「多策略 + 重试」：① 直接读整数；② 读游标左右相邻的两个刻度数字再取中点。
  同时把方位角图放大 3 倍再发（小字放大后准确率明显更高）。
- 不加 reasoning_effort，加了更慢且不准。
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.request

from PIL import Image, ImageGrab

API_BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"
MAX_TOKENS = 900

SYSTEM_PROMPT = (
    "你是军事游戏截图读数字工具。图中通常有一行文字（可能是 CURRENT GRID，也可能是"
    "「玩家位置：」之类的中文标签），后面跟着网格坐标，形如 D3-9-1、L13-3-3 或 F11-2-5-6"
    "（1 个字母 + 1~2 位数字，后面 2~3 段各 1 位数字）。"
    "只输出坐标本身：字母大写，各段用短横连接，无空格。图中没有可读坐标时只输出 NONE。"
)

COORD_RE = re.compile(r"[A-Z]\s*\d{1,2}(?:\s*-\s*\d){2,3}")

# 望远镜流程第一步：玩家视角的方位角（罗盘上那个 0–359 的数）
SYSTEM_PROMPT_AZ = (
    "你是军事游戏截图读数字工具。图中是玩家视角的方位角读数，是一个 0 到 359 的整数，"
    "通常写成 3 位数字、可能带前导 0（例如 051、089、269、180），周围可能有刻度线或 ° 符号。"
    "只输出这个整数本身，不要 °、不要单位、不要解释。图中没有可读的角度时只输出 NONE。"
)

# 方位角策略二：只读游标左右相邻的两个刻度数字（模型做「读数 + 取邻近值」比做插值稳）
SYSTEM_PROMPT_AZ_PAIR = (
    "图中是一条方位角刻度带，画面正中有一条（通常是黄色/白色的）竖线游标。"
    "只输出两个整数：游标左侧最近的刻度数字、游标右侧最近的刻度数字，中间用空格分开。"
    "不要 °、不要解释、不要其他文字。"
)

AZ_RE = re.compile(r"(?<![0-9])(\d{1,3})(?![0-9])")


class VisionError(RuntimeError):
    pass


class VisionRetry(VisionError):
    """可以重试的临时性失败：服务端 5xx / 429 / 超时 / 连不上。

    单列一个类型是因为 DeepSeek 侧偶发 500 internal_error（实测约 3%），
    退避重试一次基本就过去了；而 401/400 这种重试多少次都没用，不能混在一起。
    """
    pass


RETRY_TRIES = 3           # 一次请求最多试 3 次（首发 + 2 次重试）
RETRY_DELAYS = (0.5, 1.5)  # 第 1、2 次重试前等多少秒


def grab(region: tuple[int, int, int, int]) -> Image.Image:
    """region = (x, y, w, h)，物理像素，全屏坐标。"""
    x, y, w, h = region
    if w < 4 or h < 4:
        raise VisionError("识别区域太小，请重新框选")
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return img.convert("RGB")


def _auto_up(img, target_w: int = 640) -> int:
    """小截图放大到 ~640 px 宽再发（实测 32×17 的方位角小图放 6 倍准确率明显更高）。"""
    w = max(1, int(img.width))
    return max(1, min(12, int(round(target_w / w))))


def _b64(img, upscale: int = 1) -> str:
    if upscale > 1:
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _payload(img, system: str, ask: str, upscale: int = 1) -> dict:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ask},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + _b64(img, upscale)}},
                ],
            },
        ],
    }


def _post(payload: dict, api_key: str, timeout: float) -> tuple[str, str, int]:
    """发一次请求 -> (content, finish_reason, 毫秒)。网络/HTTP 错误抛 VisionError。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        msg = f"接口返回 {e.code}：{detail}"
        if e.code >= 500 or e.code in (408, 429):
            raise VisionRetry(msg) from None
        raise VisionError(msg) from None
    except urllib.error.URLError as e:
        raise VisionRetry(f"连不上 api.deepseek.com：{e.reason}") from None
    except TimeoutError:
        raise VisionRetry(f"请求超时（{timeout:g} 秒）") from None
    ms = int((time.time() - t0) * 1000)
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError):
        raise VisionError("接口返回格式异常：" + json.dumps(data, ensure_ascii=False)[:300]) from None
    content = (msg.get("content") or "").strip()
    return content, str(choice.get("finish_reason") or ""), ms


def _post_retry(payload: dict, api_key: str, timeout: float,
                tries: int = RETRY_TRIES, delays: tuple[float, ...] = RETRY_DELAYS):
    """带退避的重试：只重试 VisionRetry（5xx / 429 / 超时 / 连不上）。

    401 这种硬错误直接抛，不浪费用户 3 倍等待时间。
    """
    tries = max(1, int(tries))
    last: VisionRetry | None = None
    for i in range(tries):
        try:
            return _post(payload, api_key, timeout)
        except VisionRetry as e:
            last = e
            if i >= tries - 1:
                break
            delay = delays[min(i, len(delays) - 1)] if delays else 0.0
            try:
                print(f"[ocr] 接口临时失败（{e}），{delay:g}s 后重试 {i + 1}/{tries - 1}", flush=True)
            except Exception:
                pass
            time.sleep(delay)
    raise VisionError(str(last)) from None


def parse_reply(text: str) -> str | None:
    """从模型回复里抠出规范坐标（3 段或 4 段）；没有则 None。"""
    if not text:
        return None
    for m in COORD_RE.finditer(text.upper()):
        parts = [p.strip() for p in m.group(0).split("-")]
        if len(parts) not in (3, 4):
            continue
        letter, row = parts[0][0], parts[0][1:]
        rest = "-".join(str(int(p)) for p in parts[1:])
        return f"{letter.upper()}{int(row)}-{rest}"
    return None


def parse_az(text: str) -> str | None:
    """从模型回复里抠出方位角（0–359 的整数）；没有则 None。"""
    if not text:
        return None
    up = text.upper()
    if "NONE" in up:
        return None
    for m in AZ_RE.finditer(up):
        try:
            v = int(m.group(1))
        except ValueError:
            continue
        if 0 <= v <= 359:
            return str(v % 360)
    return None


def parse_az_pair(text: str) -> str | None:
    """「左 右 相邻刻度」-> 取中点；两个数不成对（差 >10）则 None。"""
    if not text:
        return None
    nums = []
    for m in AZ_RE.finditer(text.upper()):
        try:
            v = int(m.group(1))
        except ValueError:
            continue
        if 0 <= v <= 360:
            nums.append(v)
    if len(nums) < 2:
        return None
    a, b = nums[0], nums[1]
    d = (b - a) % 360
    if d > 180:
        d -= 360
    if abs(d) > 10:          # 不是相邻的两个刻度，别瞎猜
        return None
    mid = (a + d / 2.0) % 360
    return str(int(round(mid)) % 360)


_NONE_HINT = "模型没给出结果"


def ask(img, api_key: str, kind: str = "grid", timeout: float = 30.0) -> tuple[str | None, str, int]:
    """返回 (坐标/方位角 或 None, 原始回复, 毫秒)。kind: grid=网格坐标, az=方位角。"""
    if not api_key:
        raise VisionError("还没有填 DeepSeek 密钥")
    if kind == "az":
        plans = [
            (SYSTEM_PROMPT_AZ, "读出方位角。", 0, parse_az),
            (SYSTEM_PROMPT_AZ, "读出方位角。", 0, parse_az),
            (SYSTEM_PROMPT_AZ_PAIR, "读出游标左右的刻度数字。", 0, parse_az_pair),
        ]
    else:
        plans = [
            (SYSTEM_PROMPT, "读出网格坐标。", 0, parse_reply),
            (SYSTEM_PROMPT, "读出网格坐标。", 1, parse_reply),
        ]

    t0 = time.time()
    last_content = ""
    auto_up = _auto_up(img)
    for system, ask_text, up, parser in plans:
        try:
            content, _reason, _ms = _post_retry(
                _payload(img, system, ask_text, up or auto_up), api_key, timeout)
        except VisionError:
            raise
        if content:
            last_content = content
            got = parser(content)
            if got:
                return got, content, int((time.time() - t0) * 1000)
        else:
            last_content = last_content or ""

    ms = int((time.time() - t0) * 1000)
    if last_content:
        # 模型答了但没读到要的东西（比如 NONE）：把原文交给上层提示
        return None, last_content, ms
    raise VisionError(f"{_NONE_HINT}（content 为空，可能是推理被 max_tokens 截断），请重试")
