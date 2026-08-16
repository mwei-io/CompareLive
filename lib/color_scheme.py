import os
import json
import sublime

# 插件目录
PKG_DIR = os.path.dirname(__file__)

# 各高亮 scope 与其对应的设置键映射表
_SCOPE_MAP = [
    ("markup.inserted.compare-live", "added_bg"),       # 新增行背景色
    ("markup.deleted.compare-live", "removed_bg"),      # 删除行背景色
    ("markup.changed.compare-live", "changed_bg"),      # 修改行背景色
    ("markup.changed.char.compare-live", "changed_char_bg"),   # 修改行内字符背景色
]

# 颜色默认值字典（参考 IntelliJ IDEA Darcula 差异对比配色）
# 修改行采用双层高亮：整行淡底色 + 字符级重色，确保层次分明
DEFAULT_COLORS = {
    "added_bg": "#2d4f34",          # 新增行：暗绿色背景
    "removed_bg": "#4b2e2e",        # 删除行：暗红色背景
    "changed_bg": "#384d65",        # 修改行：中等蓝色底色（深色背景下可辨识）
    "changed_char_bg": "#4e6d8b",   # 修改行内字符：鲜明蓝色背景（视觉焦点，两侧统一）
    "foreground": "",               # 前景色：空表示沿用编辑器默认前景色
}

# 记录当前已生成的 overlay 文件名（用于切换 scheme 时清理旧文件）
_current_overlay_name = None


def _valid_color(value):
    """粗略校验颜色值是否为可用的颜色字符串。

    支持以下格式：
    - 十六进制：#RGB / #RRGGBB / #RRGGBBAA
    - CSS 函数：rgb() / rgba() / hsl() / hsla()
    - CSS 颜色名：纯字母字符串
    """
    if not isinstance(value, str) or not value:
        return False
    v = value.strip()
    if v.startswith("#") and len(v) in (4, 7, 9):
        return True
    return v.startswith(("rgb", "hsl")) or v.isalpha()


def _build_rules(colors):
    """依据颜色字典构建颜色方案的 rules 列表。"""
    fg = colors.get("foreground") or ""
    rules = []
    for scope, key in _SCOPE_MAP:
        bg = colors.get(key) or DEFAULT_COLORS[key]
        if not _valid_color(bg):
            bg = DEFAULT_COLORS[key]
        rule = {"scope": scope, "background": bg}
        if fg and _valid_color(fg):
            rule["foreground"] = fg
        rules.append(rule)
    return rules


def _get_scheme_filename():
    """获取用户当前激活的颜色方案文件名。

    从 Preferences 中读取 color_scheme 设置，提取文件名部分。
    对于 .tmTheme 格式，转换为 .sublime-color-scheme 后缀。

    返回:
        str: 颜色方案文件名（带 .sublime-color-scheme 后缀）
    """
    prefs = sublime.load_settings("Preferences.sublime-settings")
    scheme_path = prefs.get("color_scheme", "")
    if not scheme_path:
        return "Mariana.sublime-color-scheme"  # 默认回退

    # 提取文件名
    filename = os.path.basename(scheme_path)

    # .tmTheme 格式转换为 .sublime-color-scheme 后缀
    base, ext = os.path.splitext(filename)
    if ext.lower() == ".tmtheme":
        filename = base + ".sublime-color-scheme"

    return filename


def _cleanup_old_overlay(exclude_name=None):
    """清理插件目录中的旧 overlay 文件。

    删除除 exclude_name 之外的所有由本插件生成的 .sublime-color-scheme 文件。
    通过读取文件内容中的特征字段判断是否为本插件生成。
    """
    global _current_overlay_name
    for f in os.listdir(PKG_DIR):
        if not f.endswith(".sublime-color-scheme"):
            continue
        if f == exclude_name:
            continue
        fpath = os.path.join(PKG_DIR, f)
        # 检查是否为本插件生成的文件（通过 name 字段识别）
        try:
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.loads(fp.read())
            if data.get("name") == "CompareLive Highlights":
                os.remove(fpath)
        except Exception:
            pass
    _current_overlay_name = exclude_name


def generate(colors, force=False):
    """生成与用户当前 color scheme 同名的 overlay 文件。

    Sublime Text 的颜色方案合并机制要求 overlay 文件名必须与
    用户当前激活的 color scheme 文件名相同，才能被合并。

    参数:
        colors: 颜色配置字典
        force:  是否强制写入（跳过内容比对）

    返回:
        str: 写入的文件绝对路径，失败时返回 None
    """
    global _current_overlay_name

    # 获取用户当前配色方案文件名
    target_name = _get_scheme_filename()

    # 清理旧的 overlay 文件（名称可能已变，或首次启动时清理残留文件）
    if _current_overlay_name != target_name:
        _cleanup_old_overlay(exclude_name=target_name)

    # 构建 overlay 内容
    scheme = {
        "name": "CompareLive Highlights",
        "variables": {},
        "globals": {},
        "rules": _build_rules(colors),
    }
    content = json.dumps(scheme, ensure_ascii=False, indent=4)

    path = os.path.join(PKG_DIR, target_name)

    # 非强制模式下，内容相同则跳过
    if not force:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    if f.read() == content:
                        _current_overlay_name = target_name
                        return path
        except Exception:
            pass

    # 写入 overlay 文件
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print("[CompareLive] 写入配色方案 overlay 失败: {}".format(e))
        return None

    _current_overlay_name = target_name
    return path


def cleanup():
    """插件卸载时清理所有生成的 overlay 文件。"""
    _cleanup_old_overlay(exclude_name=None)
