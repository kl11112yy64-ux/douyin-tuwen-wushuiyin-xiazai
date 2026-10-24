#!/usr/bin/env python3
"""下载抖音图文帖中未添加平台下载水印的图片。

用法：
    python3 douyin_note_downloader.py "https://www.douyin.com/note/..."
    python3 douyin_note_downloader.py "链接" -o "输出目录" --no-zip

依赖：
    1. 本机已安装并连接 Kimi WebBridge 浏览器扩展；
    2. 系统可使用 curl（macOS 通常自带）；
    3. Python 3.9+，不需要第三方 Python 包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BRIDGE_URL = "http://127.0.0.1:10086/command"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def bridge_call(action: str, args: dict[str, Any], session: str) -> dict[str, Any]:
    body = json.dumps(
        {"action": action, "args": args, "session": session},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        BRIDGE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"浏览器桥接请求失败（HTTP {exc.code}）：{detail}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"浏览器操作失败：{str(result)[:800]}")
    return result.get("data") or {}


def start_bridge() -> None:
    executable = Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge"
    if not executable.exists():
        raise RuntimeError(
            "未找到 Kimi WebBridge。请先安装并连接浏览器扩展："
            "https://www.kimi.com/zh-cn/features/webbridge"
        )
    result = subprocess.run(
        [str(executable), "start"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法启动浏览器桥接：{result.stderr.strip()}")
    time.sleep(2)


def navigate(url: str, session: str) -> None:
    args = {"url": url, "newTab": True, "group_title": "抖音图文下载"}
    try:
        bridge_call("navigate", args, session)
    except urllib.error.URLError:
        start_bridge()
        try:
            bridge_call("navigate", args, session)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "无法连接本机浏览器桥接，请确认浏览器扩展已启用。"
            ) from exc


def wait_for_embedded_data(session: str, timeout: int = 60) -> dict[str, Any]:
    code = r"""(() => {
      const scripts = Array.from(document.scripts)
        .map(s => s.textContent)
        .filter(t => t.includes('self.__pace_f.push(')
          && t.includes('awemeId')
          && t.includes('urlList')
          && t.includes('images'));
      return JSON.stringify({url: location.href, title: document.title, scripts});
    })()"""
    deadline = time.monotonic() + timeout
    last_info: dict[str, Any] = {}
    while time.monotonic() < deadline:
        data = bridge_call("evaluate", {"code": code}, session)
        value = data.get("value", "{}")
        info = json.loads(value) if isinstance(value, str) else value
        last_info = info
        if info.get("scripts"):
            return info
        time.sleep(2)
    raise RuntimeError(
        "等待后仍未找到图文数据。请检查帖子是否可公开访问、是否需要在浏览器登录，"
        f"或页面是否出现验证码。当前页面：{last_info.get('url', '未知')}"
    )


def parse_flight_script(text: str) -> Any | None:
    marker = "self.__pace_f.push("
    if marker not in text:
        return None
    start = text.index(marker) + len(marker)
    end = text.rindex(")")
    try:
        envelope = json.loads(text[start:end])
        chunk = envelope[1]
        return json.loads(chunk[chunk.index(":") + 1 :])
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def raw_urls(image: dict[str, Any]) -> list[str]:
    urls = image.get("urlList") or image.get("url_list") or []
    if not isinstance(urls, list):
        return []
    return [
        url
        for url in urls
        if isinstance(url, str)
        and url.startswith(("https://", "http://"))
        and "water" not in url.lower()
    ]


def find_image_lists(value: Any, found: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
    if isinstance(value, dict):
        images = value.get("images")
        if (
            isinstance(images, list)
            and images
            and all(isinstance(image, dict) for image in images)
            and any(raw_urls(image) for image in images)
        ):
            found.append((value, images))
        for child in value.values():
            find_image_lists(child, found)
    elif isinstance(value, list):
        for child in value:
            find_image_lists(child, found)


def find_aweme_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("awemeId", "aweme_id"):
            item = value.get(key)
            if isinstance(item, (str, int)) and re.fullmatch(r"\d+", str(item)):
                return str(item)
        for child in value.values():
            result = find_aweme_id(child)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_aweme_id(child)
            if result:
                return result
    return None


def extract_images(scripts: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    aweme_id: str | None = None
    for script in scripts:
        data = parse_flight_script(script)
        if data is None:
            continue
        aweme_id = aweme_id or find_aweme_id(data)
        find_image_lists(data, candidates)
    if not candidates:
        raise RuntimeError("作品数据已加载，但没有解析出图片地址；页面数据结构可能已变化。")
    detail, images = max(
        candidates,
        key=lambda item: (len(item[1]), bool(item[0].get("desc"))),
    )
    return detail, images, aweme_id


def safe_title(detail: dict[str, Any], note_id: str) -> str:
    desc = detail.get("desc")
    title = str(desc).splitlines()[0].strip() if desc else f"douyin_{note_id}"
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title)
    title = title[:48].strip().strip(".").strip()
    return title or f"douyin_{note_id}"


def image_size(data: bytes) -> tuple[str, tuple[int, int] | None]:
    if data.startswith(b"\xff\xd8"):
        index = 2
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0x00, 0x01} or 0xD0 <= marker <= 0xD9:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index : index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in sof and length >= 7:
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return "JPEG", (width, height)
            index += length
        raise ValueError("JPEG 文件中没有找到有效尺寸")
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return "PNG", (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return "GIF", (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return "WEBP", (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return "WEBP", (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "WEBP", ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        return "WEBP", None
    raise ValueError("下载内容不是受支持的图片")


def inspect_image(path: Path, expected: tuple[int, int] | None) -> tuple[str, tuple[int, int] | None]:
    data = path.read_bytes()
    if len(data) < 100:
        raise ValueError("图片文件过小")
    image_format, size = image_size(data)
    if expected and size and size != expected:
        raise ValueError(f"尺寸不符：期望 {expected}，实际 {size}")
    return image_format, size


def expected_size(image: dict[str, Any]) -> tuple[int, int] | None:
    try:
        width, height = int(image.get("width")), int(image.get("height"))
        return (width, height) if width > 0 and height > 0 else None
    except (TypeError, ValueError):
        return None


def sorted_urls(image: dict[str, Any]) -> list[str]:
    def rank(url: str) -> int:
        path = urlparse(url).path.lower()
        return 0 if path.endswith((".jpg", ".jpeg")) else 1

    return sorted(raw_urls(image), key=rank)


def download_one(
    image: dict[str, Any], index: int, digits: int, output: Path, referer: str
) -> tuple[Path, str, tuple[int, int] | None, bool]:
    expected = expected_size(image)
    prefix = f"{index:0{digits}d}"
    for old in output.glob(f"{prefix}.*"):
        if old.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            image_format, size = inspect_image(old, expected)
            return old, image_format, size, True
        except ValueError:
            old.unlink()

    urls = sorted_urls(image)
    if not urls:
        raise RuntimeError("没有不带下载水印的展示图地址")
    errors: list[str] = []
    for url in urls:
        temp = output / f"{prefix}.part"
        result = subprocess.run(
            [
                "curl", "-sS", "-L", "--fail", "--max-time", "45",
                "-A", USER_AGENT, "-e", referer, "-o", str(temp), url,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            errors.append(result.stderr.strip() or f"curl 退出码 {result.returncode}")
            temp.unlink(missing_ok=True)
            continue
        try:
            image_format, size = inspect_image(temp, expected)
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}[image_format]
            target = output / f"{prefix}{suffix}"
            temp.replace(target)
            return target, image_format, size, False
        except ValueError as exc:
            errors.append(str(exc))
            temp.unlink(missing_ok=True)
    raise RuntimeError("；".join(error for error in errors if error) or "所有图床地址均不可用")


def make_zip(output: Path, files: list[Path]) -> Path:
    archive = Path(str(output) + ".zip")
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file in files:
            bundle.write(file, arcname=f"{output.name}/{file.name}")
    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        if bad:
            raise RuntimeError(f"ZIP 校验失败：{bad}")
        if len(bundle.namelist()) != len(files):
            raise RuntimeError("ZIP 中图片数量不符")
    return archive


def extract_url(text: str) -> str:
    match = re.search(r"https?://[^\s]+", text.strip())
    if not match:
        raise ValueError("请提供有效的抖音链接")
    url = match.group(0).rstrip("，。；、）)]}>")
    host = (urlparse(url).hostname or "").lower()
    if not (host == "douyin.com" or host.endswith(".douyin.com")):
        raise ValueError("目前只支持 douyin.com 链接")
    return url


def run(args: argparse.Namespace) -> None:
    url = extract_url(args.url)
    session = f"douyin-note-download-{os.getpid()}"
    print("正在用浏览器加载帖子……")
    navigate(url, session)
    page = wait_for_embedded_data(session)
    detail, images, embedded_id = extract_images(page["scripts"])
    note_id = embedded_id or "unknown"
    match = re.search(r"/note/(\d+)", page.get("url", ""))
    if match:
        note_id = match.group(1)
    title = safe_title(detail, note_id)
    output = args.output.expanduser().resolve() if args.output else Path.cwd() / f"{title}_无水印壁纸"
    output.mkdir(parents=True, exist_ok=True)
    print(f"解析到 {len(images)} 张图片，保存目录：{output}")

    files: list[Path] = []
    failures: list[str] = []
    digits = max(2, len(str(len(images))))
    for index, image in enumerate(images, 1):
        try:
            file, image_format, size, skipped = download_one(
                image, index, digits, output, page.get("url") or url
            )
            files.append(file)
            dimensions = f"{size[0]}x{size[1]}" if size else "尺寸未知"
            state = "已存在" if skipped else "已下载"
            print(f"[{index:02d}/{len(images):02d}] {state} {file.name}｜{dimensions}｜{image_format}")
        except Exception as exc:
            failures.append(f"第 {index} 张：{exc}")
            print(f"[{index:02d}/{len(images):02d}] 失败：{exc}", file=sys.stderr)
        time.sleep(0.2)

    if failures:
        raise RuntimeError(
            f"有 {len(failures)} 张下载失败；成功文件已保留在 {output}。\n"
            + "\n".join(failures)
        )

    digest = hashlib.sha256()
    for file in files:
        digest.update(file.read_bytes())
    print(f"全部完成：{len(files)} 张，合计校验值 {digest.hexdigest()[:16]}")
    if not args.no_zip:
        archive = make_zip(output, files)
        print(f"ZIP 已生成并校验：{archive}")


def main() -> None:
    parser = argparse.ArgumentParser(description="批量下载抖音图文帖的无平台下载水印图片")
    parser.add_argument("url", help="抖音图文链接，也可以粘贴包含链接的分享文本")
    parser.add_argument("-o", "--output", type=Path, help="图片输出目录")
    parser.add_argument("--no-zip", action="store_true", help="只保存图片，不生成 ZIP")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
