"""生成启动器图标 launcher/assets/app.ico（多尺寸）。

只在构建时运行一次，产物已随项目提交；改了设计再跑：
    vendor\\index-tts\\.venv\\Scripts\\python.exe launcher\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
SIZES = (256, 128, 64, 48, 32, 16)


def build(size: int = 1024) -> Image.Image:
    s = size
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 深色圆角底（与主界面 #0a0e13 一致）
    radius = int(s * 0.23)
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=(10, 14, 19, 255))
    draw.rounded_rectangle(
        (int(s * 0.015), int(s * 0.015), s - 1 - int(s * 0.015), s - 1 - int(s * 0.015)),
        radius=int(radius * 0.95),
        outline=(31, 43, 56, 255),
        width=max(2, int(s * 0.012)),
    )

    # 蓝色主圆（品牌色 #2f81f7）
    pad = int(s * 0.16)
    circle = (pad, pad, s - pad, s - pad)
    draw.ellipse(circle, fill=(47, 129, 247, 255))

    # 均衡器波形：五根圆角竖条，中间高两侧低
    cy = s * 0.5
    bar_w = int(s * 0.058)
    gap = int(s * 0.093)
    heights = (0.24, 0.40, 0.53, 0.40, 0.24)
    start_x = s * 0.5 - (gap * (len(heights) - 1) + bar_w) / 2
    for index, ratio in enumerate(heights):
        height = s * ratio
        x = start_x + index * gap
        draw.rounded_rectangle(
            (x, cy - height / 2, x + bar_w, cy + height / 2),
            radius=bar_w / 2,
            fill=(255, 255, 255, 255),
        )
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build()
    ico = ASSETS / "app.ico"
    master.resize((256, 256), Image.LANCZOS).save(ico, format="ICO", sizes=[(n, n) for n in SIZES])
    preview = ASSETS / "app_preview.png"
    master.resize((256, 256), Image.LANCZOS).save(preview, format="PNG")
    print(f"已生成：{ico}")
    print(f"预览图：{preview}")


if __name__ == "__main__":
    main()
