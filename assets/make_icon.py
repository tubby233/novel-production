"""生成 assets/icon.ico（纯标准库实现，不依赖 Pillow）。

用法：
    python assets/make_icon.py

图标内容：深蓝圆角底 + 白色"书页 + 星形光标"极简图案，尺寸 16/32/48/64/128/256。
若你已有美术资源，直接把自己的 .ico 覆盖为 assets/icon.ico 即可。
"""

from __future__ import annotations

import struct
from pathlib import Path

BASE = (31, 111, 235, 255)      # #1f6feb
PAGE = (255, 255, 255, 255)
LINE = (182, 227, 255, 255)
STAR = (255, 214, 102, 255)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """返回 size x size 的 RGBA 像素矩阵。"""
    px = [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]
    radius = max(2, size // 5)

    # 圆角矩形底
    for y in range(size):
        for x in range(size):
            inside = True
            for cx, cy in (
                (radius, radius),
                (size - 1 - radius, radius),
                (radius, size - 1 - radius),
                (size - 1 - radius, size - 1 - radius),
            ):
                if (x < radius and y < radius and (cx, cy) == (radius, radius)) or \
                   (x > size - 1 - radius and y < radius and (cx, cy) == (size - 1 - radius, radius)) or \
                   (x < radius and y > size - 1 - radius and (cx, cy) == (radius, size - 1 - radius)) or \
                   (x > size - 1 - radius and y > size - 1 - radius and (cx, cy) == (size - 1 - radius, size - 1 - radius)):
                    if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                        inside = False
            if inside:
                px[y][x] = BASE

    # 中间白色书页
    margin = max(2, size // 5)
    page_top = margin + max(1, size // 12)
    page_bottom = size - margin
    for y in range(page_top, page_bottom):
        for x in range(margin, size - margin):
            px[y][x] = PAGE

    # 书页上的横线
    line_height = max(1, size // 22)
    for index in range(3):
        y0 = page_top + int((page_bottom - page_top) * (0.22 + index * 0.22))
        for y in range(y0, min(page_bottom, y0 + line_height)):
            start = margin + max(1, size // 10)
            end = size - margin - max(1, size // 10)
            for x in range(start, end):
                px[y][x] = LINE

    # 右下角星形（代表"生成/AI"）
    star_r = max(2, size // 7)
    cx = size - margin - star_r // 2
    cy = size - margin - star_r // 2
    for y in range(max(0, cy - star_r), min(size, cy + star_r + 1)):
        for x in range(max(0, cx - star_r), min(size, cx + star_r + 1)):
            dx, dy = abs(x - cx), abs(y - cy)
            if dx + dy <= star_r or (dx <= star_r // 3 and dy <= star_r) or (dy <= star_r // 3 and dx <= star_r):
                px[y][x] = STAR
    return px


def encode_png(size: int, pixels) -> bytes:
    """把像素矩阵编码成 PNG（仅用 zlib，无第三方依赖）。"""
    import zlib

    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def write_ico(path: Path) -> None:
    images = [(size, encode_png(size, render(size))) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    offset = 6 + 16 * len(images)
    payload = bytearray()
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + bytes(payload))
    print(f"已生成 {path}（{len(images)} 个尺寸：{', '.join(str(s) for s in SIZES)}）")


if __name__ == "__main__":
    write_ico(Path(__file__).resolve().parent / "icon.ico")
