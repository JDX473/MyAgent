"""Feidudu 图片 Logo：加载 res/feidudu.png 并转成 ANSI 真彩色半块图。

TUI 启动时显示这张图片。精细化的关键：
  1. 去掉近白背景（feidudu.png 是白底），铺到深色 TUI 底上
  2. 用足够高的分辨率（字符宽较大），减少缩放的细节损失
  3. 缩放用 LANCZOS，颜色做简单量化减少色带

依赖 Pillow（pip install pillow）。未安装或图片缺失时回退到 ASCII banner。
"""
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(_BASE_DIR, "res", "feidudu.png")

_LOGO_AVAILABLE = True
try:
    from PIL import Image as _PILImage
except ImportError:
    _LOGO_AVAILABLE = False

# 近白判断阈值：RGB 都 > 此值视为白底（去除）
_WHITE_THRESHOLD = 238
# 半块字符 ▀ 在终端的高/宽比约为 2
_CHAR_ASPECT = 2.0


def _is_near_white(rgb) -> bool:
    r, g, b = rgb[:3]
    return r >= _WHITE_THRESHOLD and g >= _WHITE_THRESHOLD and b >= _WHITE_THRESHOLD


def _remove_white_bg(img):
    """把近白背景像素设为透明，保留主体。"""
    from PIL import Image as Img
    rgba = img.convert("RGBA")
    px = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, a = px[x, y]
            if _is_near_white((r, g, b)):
                px[x, y] = (r, g, b, 0)
    return rgba


def _flatten_on_dark(img):
    """把（可能透明的）图铺到深色底上，得到纯 RGB。"""
    from PIL import Image as Img
    bg = Img.new("RGBA", img.size, (15, 17, 21, 255))  # 深色底，同 TUI 背景
    bg.alpha_composite(img)
    return bg.convert("RGB")


def _dark_theme_adjust(img, bg: tuple = (15, 17, 21)) -> "Image.Image":
    """把浅色背景的 logo 调成适配深色 TUI 的配色（黄色袋鼠版）。

    feidudu.png 是浅灰底(#e0e0e0) + 黄色袋鼠 + 黑眼睛/鼻子/轮廓。
    处理：
      - 近白/浅灰背景 → 深色底，融入 TUI 背景
      - 黄色主体 → 提亮保真，在深底上醒目
      - 黑色特征（眼睛/鼻子/轮廓）→ **保持纯黑**，在黄色袋鼠身上清晰可见
    关键：黑色不提亮（提亮会变成灰，深底上糊掉 → 眼睛消失、鼻子不黑）。
    """
    from PIL import Image as Img
    rgb = img.convert("RGB")
    px = rgb.load()
    out = Img.new("RGB", rgb.size)
    op = out.load()

    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            lum = (r + g + b) / 3
            sat = mx - mn

            if lum > 195 and sat < 70:
                # 近白/浅灰背景 → 深色底
                op[x, y] = bg
            elif sat > 35 and mx > 140:
                # 彩色主体（黄色袋鼠）→ 提亮保真
                op[x, y] = (min(255, r + 25), min(255, g + 25), min(255, b + 15))
            elif lum < 90:
                # 黑色特征（眼睛/鼻子/轮廓）→ 保持纯黑（黑色在黄色身上可见）
                op[x, y] = (0, 0, 0)
            else:
                # 中间色：偏亮当作背景压暗，否则保留
                op[x, y] = bg if lum > 165 else (r, g, b)

    return out


def logo_ansi(width: int = 60, max_height: int = 28) -> str | None:
    """生成 Feidudu 图片 logo 的 ANSI 版本；加载失败返回 None。"""
    img = load_logo_image()
    if img is None:
        return None
    # 深色主题化：浅灰底融入深色背景，主体提亮
    img = _dark_theme_adjust(img)
    return image_to_ansi(img, width, max_height)


def image_to_ansi(img, width: int = 60, max_height: int = 28) -> str:
    """把 PIL 图片转成 ANSI 真彩色半块字符。

    width：目标字符宽度（越大越精细，但占屏越多）。
    max_height：字符高度上限。
    """
    aspect = img.height / img.width
    height_char = max(1, round(width * aspect / _CHAR_ASPECT))
    height_char = min(height_char, max_height)

    # 像素画布：宽 = width 字符，高 = 2 * height_char 像素
    img = img.resize((width, height_char * 2), _PILImage.Resampling.LANCZOS)

    lines = []
    for y in range(0, img.height, 2):
        row = []
        for x in range(img.width):
            r1, g1, b1 = img.getpixel((x, y))
            if y + 1 < img.height:
                r2, g2, b2 = img.getpixel((x, y + 1))
            else:
                r2 = g2 = b2 = 0
            row.append(f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m▀")
        lines.append("".join(row) + "\x1b[0m")
    return "\n".join(lines)


def load_logo_image():
    """加载 res/feidudu.png，返回 PIL Image；失败返回 None。"""
    if not _LOGO_AVAILABLE:
        return None
    try:
        from PIL import Image as Img
        return Img.open(LOGO_PATH).convert("RGBA")
    except Exception:
        return None


def logo_available() -> bool:
    """Pillow 是否可用且图片存在。"""
    return _LOGO_AVAILABLE and os.path.exists(LOGO_PATH) and load_logo_image() is not None
