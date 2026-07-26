import sublime
import sublime_plugin
import os
import json

from . import diff_engine
from . import color_scheme

# 设置文件名
SETTINGS_FILE = "CompareLive.sublime-settings"

# 联动滚动轮询间隔(毫秒)
SCROLL_INTERVAL_MS = 40

# ---- 多语言翻译字典 ----
_I18N = {
    "zh_CN": {
        "status.base": "[CompareLive] 基准 · 只读 ◀",
        "status.compare": "[CompareLive] 比较 · 可编辑 ▶",
        "msg.started": "[CompareLive] 已在新窗口中开始并排比较",
        "msg.no_other_tabs": "[CompareLive] 没有其它可比较的标签",
        "msg.need_views": "[CompareLive] 需要两个有效的视图",
        "msg.need_different": "[CompareLive] 请选择两个不同的标签/内容进行比较",
        "msg.no_session": "[CompareLive] 当前没有活跃的比较会话",
        "msg.ended": "[CompareLive] 已结束比较",
        "label.untitled": "未命名",
        "menu.compare_with": "与选定标签比较",
        "menu.end": "结束比较",
        "menu.end_current": "结束当前比较",
        "cmd.compare_with": "CompareLive: 与选定标签比较",
        "cmd.end": "CompareLive: 结束比较",
        "cmd.settings": "Preferences: CompareLive 设置",
        "warn.language": "language 仅支持 'zh_CN'/'en'，已回退 'zh_CN'",
        "warn.base_readonly": "base_readonly 应为布尔值，已回退 true",
        "warn.colors": "colors 应为对象，已使用默认配色",
        "warn.prefix": "[CompareLive] 设置校验提示: ",
    },
    "en": {
        "status.base": "[CompareLive] Base · Read-only ◀",
        "status.compare": "[CompareLive] Compare · Editable ▶",
        "msg.started": "[CompareLive] Side-by-side compare started",
        "msg.no_other_tabs": "[CompareLive] No other tabs to compare",
        "msg.need_views": "[CompareLive] Two valid views required",
        "msg.need_different": "[CompareLive] Please select two different tabs",
        "msg.no_session": "[CompareLive] No active compare session",
        "msg.ended": "[CompareLive] Compare ended",
        "label.untitled": "Untitled",
        "menu.compare_with": "Compare with Selected Tab",
        "menu.end": "End Compare",
        "menu.end_current": "End Current Compare",
        "cmd.compare_with": "CompareLive: Compare with Selected Tab",
        "cmd.end": "CompareLive: End Compare",
        "cmd.settings": "Preferences: CompareLive Settings",
        "warn.language": "language only supports 'zh_CN'/'en', fallback to 'zh_CN'",
        "warn.base_readonly": "base_readonly should be boolean, fallback to true",
        "warn.colors": "colors should be an object, using default colors",
        "warn.prefix": "[CompareLive] Settings validation: ",
    },
}


def _t(key):
    """根据当前语言配置获取翻译文本。"""
    lang = sublime.load_settings(SETTINGS_FILE).get("language", "zh_CN")
    return _I18N.get(lang, _I18N["zh_CN"]).get(key, key)


# ---- 高亮 scope（与 color_scheme.py 中生成的规则一一对应） ----
SCOPE_ADDED = "markup.inserted.compare-live"          # 新增行(右侧)
SCOPE_REMOVED = "markup.deleted.compare-live"         # 删除行(左侧)
SCOPE_CHANGED = "markup.changed.compare-live"         # 修改行(两侧)
SCOPE_CHANGED_CHAR = "markup.changed.char.compare-live"   # 修改行内字符(两侧统一)

# ---- add_regions 使用的 region key ----
RK_LINE = "clv_line"       # 纯删除/纯新增整行高亮区域键
RK_LINE_CHG = "clv_lchg"   # 修改行高亮区域键
RK_CHAR = "clv_char"       # 修改行内字符级高亮区域键

# ---- 幽灵占位行 PhantomSet key ----
PK_GAP = "clv_gap"     # Phantom 占位行键名

# 两列布局定义（用于比较窗口内部的左右分栏）
LAYOUT_TWO_COL = {
    "cols": [0.0, 0.5, 1.0],
    "rows": [0.0, 1.0],
    "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
}

# 会话表：compare_window.id() -> CompareSession
_sessions = {}


# =============================================================================
# 配置读取与校验
# =============================================================================
def get_config():
    """读取并校验设置，返回规范化后的配置字典。

    从 CompareLive.sublime-settings 加载设置项，对每个值进行类型和范围校验，
    非法值回退默认并在控制台打印告警信息。

    返回:
        dict: 包含所有配置项的规范化字典
    """
    s = sublime.load_settings(SETTINGS_FILE)
    warn = []

    # 语言配置校验（_t 读取设置时自带回退，此处仅校验合法性并告警）
    if s.get("language", "zh_CN") not in ("zh_CN", "en"):
        warn.append(_t("warn.language"))

    # 基准侧只读校验
    base_readonly = s.get("base_readonly", True)
    if not isinstance(base_readonly, bool):
        warn.append(_t("warn.base_readonly"))
        base_readonly = True

    # 颜色配置合并：用户设置覆盖默认值
    colors = dict(color_scheme.DEFAULT_COLORS)
    user_colors = s.get("colors", {})
    if isinstance(user_colors, dict):
        colors.update({k: v for k, v in user_colors.items() if v})
    else:
        warn.append(_t("warn.colors"))

    if warn:
        print(_t("warn.prefix") + "; ".join(warn))

    return {
        "base_readonly": base_readonly,
        "colors": colors,
    }


# =============================================================================
# 会话对象
# =============================================================================
class CompareSession(object):
    """记录一次并排比较的全部运行期状态。

    每次发起比较时创建一个 CompareSession 实例，保存比较窗口、原始窗口、
    左右视图引用、行映射表等运行时数据。

    属性:
        original_window: 发起比较的原始窗口引用
        compare_window:  新建的独立比较窗口引用
        base_view:       左侧基准视图(只读)
        compare_view:    右侧比较视图(可编辑)
        prev_readonly:   比较前 base_view 的只读状态(用于恢复)
        active:          会话是否仍在活跃状态
    """

    def __init__(self, original_window, compare_window, base_view, compare_view):
        """初始化比较会话。

        参数:
            original_window: 发起比较的原始窗口
            compare_window:  新建的比较窗口
            base_view:       左侧基准视图
            compare_view:    右侧比较视图
        """
        self.original_window = original_window
        self.compare_window = compare_window
        self.base_view = base_view
        self.compare_view = compare_view
        self.prev_readonly = base_view.is_read_only()
        self.left2right = []                # 左→右 行映射数组
        self.right2left = []                # 右→左 行映射数组
        self.base_phantoms = sublime.PhantomSet(base_view, PK_GAP)
        self.compare_phantoms = sublime.PhantomSet(compare_view, PK_GAP)
        self.active = True                  # 会话活跃标志
        self._closing = False               # 防重入：正在执行关闭流程
        # 滚动同步状态
        self.syncing = False
        self.last_base_vp = base_view.viewport_position()
        self.last_comp_vp = compare_view.viewport_position()
        self._render_token = 0              # 去抖渲染令牌

    def contains(self, view):
        """判断给定视图是否属于本会话（左侧或右侧）。"""
        return view in (self.base_view, self.compare_view)

    def schedule_rerender(self, delay=120):
        """去抖地重算并刷新差异（用于实时编辑联动）。

        每次调用时递增令牌，只有最后一次调用对应的令牌才会真正执行渲染，
        避免高频编辑时频繁重算造成性能问题。

        参数:
            delay: 延迟毫秒数，默认 120ms
        """
        self._render_token += 1
        token = self._render_token

        def _do():
            if self.active and self._render_token == token:
                render_session(self)

        sublime.set_timeout(_do, delay)


def session_for_view(view):
    """根据视图查找其所属的活跃会话。

    遍历所有活跃会话，检查给定视图是否为某个会话的左侧或右侧视图。

    参数:
        view: 待查找的 sublime.View

    返回:
        CompareSession 或 None
    """
    for sess in _sessions.values():
        if sess.active and sess.contains(view):
            return sess
    return None


def session_for_window(window):
    """根据窗口查找其对应的活跃会话。

    参数:
        window: 待查找的 sublime.Window

    返回:
        CompareSession 或 None
    """
    sess = _sessions.get(window.id())
    if sess and sess.active:
        return sess
    return None


# =============================================================================
# 渲染：对齐 -> 高亮 -> 幽灵占位行
# =============================================================================
def view_lines(view):
    """获取视图的全部文本行列表。

    参数:
        view: sublime.View 实例

    返回:
        list[str]: 按换行符分割的行列表
    """
    text = view.substr(sublime.Region(0, view.size()))
    return text.split("\n")


def render_session(sess):
    """执行一次完整的差异渲染流程。

    包括：计算对齐表、应用高亮、生成幽灵占位行。
    如果任一视图已失效或会话正在关闭，则安全退出。

    参数:
        sess: CompareSession 实例
    """
    if sess._closing or not sess.active:
        return
    if not (sess.base_view.is_valid() and sess.compare_view.is_valid()):
        end_session(sess.compare_window, restore=True)
        return

    base, comp = sess.base_view, sess.compare_view
    left_lines = view_lines(base)
    right_lines = view_lines(comp)

    rows = diff_engine.compute_alignment(left_lines, right_lines)
    sess.left2right, sess.right2left = diff_engine.build_row_maps(
        rows, len(left_lines), len(right_lines))

    _apply_highlights(sess, rows, left_lines, right_lines)
    _apply_gaps(sess, rows)


def _full_line_region(view, line_idx):
    """获取指定行号的完整行区域（含换行符）。

    参数:
        view:     sublime.View 实例
        line_idx: 行号（0-based）

    返回:
        sublime.Region: 该行的完整区域
    """
    return view.full_line(view.text_point(line_idx, 0))


def _apply_highlights(sess, rows, left_lines, right_lines):
    """根据对齐表对左右视图应用行级和字符级高亮。

    - 删除行(左侧)用"删除"色(SCOPE_REMOVED)
    - 新增行(右侧)用"新增"色(SCOPE_ADDED)
    - 修改行两侧均用"修改"色(SCOPE_CHANGED)，行内字符差异使用独立标注

    参数:
        sess:        CompareSession 实例
        rows:        对齐映射表
        left_lines:  左侧文本行列表
        right_lines: 右侧文本行列表
    """
    base, comp = sess.base_view, sess.compare_view

    # 分别收集纯删除/纯新增行 与 修改行
    base_del_lines, base_chg_lines = [], []
    comp_add_lines, comp_chg_lines = [], []
    base_chars, comp_chars = [], []

    for l, r, tag in rows:
        if tag == diff_engine.TAG_DELETE and l is not None:
            base_del_lines.append(_full_line_region(base, l))
        elif tag == diff_engine.TAG_INSERT and r is not None:
            comp_add_lines.append(_full_line_region(comp, r))
        elif tag == diff_engine.TAG_REPLACE and l is not None and r is not None:
            base_chg_lines.append(_full_line_region(base, l))
            comp_chg_lines.append(_full_line_region(comp, r))
            # 行内字符级差异高亮
            lr, rr = diff_engine.intraline_diff(left_lines[l], right_lines[r])
            lbase = base.text_point(l, 0)
            for s, e in lr:
                base_chars.append(sublime.Region(lbase + s, lbase + e))
            rbase = comp.text_point(r, 0)
            for s, e in rr:
                comp_chars.append(sublime.Region(rbase + s, rbase + e))

    flags = sublime.DRAW_NO_OUTLINE  # 仅填充背景色，不描边

    # 左侧(基准)：纯删除行用"删除"色，修改行用"修改"色
    base.add_regions(RK_LINE, base_del_lines, SCOPE_REMOVED, "", flags)
    base.add_regions(RK_LINE_CHG, base_chg_lines, SCOPE_CHANGED, "", flags)
    base.add_regions(RK_CHAR, base_chars, SCOPE_CHANGED_CHAR, "", flags)
    # 右侧(比较)：纯新增行用"新增"色，修改行用"修改"色
    comp.add_regions(RK_LINE, comp_add_lines, SCOPE_ADDED, "", flags)
    comp.add_regions(RK_LINE_CHG, comp_chg_lines, SCOPE_CHANGED, "", flags)
    comp.add_regions(RK_CHAR, comp_chars, SCOPE_CHANGED_CHAR, "", flags)


def _spacer_html(n):
    """生成幽灵占位行的 HTML 内容。

    参数:
        n: 需要撑高的行数

    返回:
        str: Phantom 使用的 HTML 字符串
    """
    row = '<div>&nbsp;</div>'
    return ('<body id="compare-live-spacer"><style>div{margin:0;padding:0;}'
            '</style>' + (row * max(n, 1)) + '</body>')


def _gap_anchor(view, line_idx):
    """返回在指定行之前插入占位块的锚点位置。

    参数:
        view:     sublime.View 实例
        line_idx: 行号；-1 表示文件末尾

    返回:
        int: 锚点的字符偏移量
    """
    if line_idx == -1:
        return view.size()
    if line_idx <= 0:
        return 0
    prev_start = view.text_point(line_idx - 1, 0)
    return view.line(prev_start).end()


def _apply_gaps(sess, rows):
    """根据对齐表为左右视图生成幽灵占位行(Phantom)实现视觉对齐。

    当一侧有行而另一侧无对应行时，在缺失侧插入等高的空白 Phantom，
    使两侧在视觉上保持行号对齐。

    参数:
        sess: CompareSession 实例
        rows: 对齐映射表
    """
    base, comp = sess.base_view, sess.compare_view
    left_gaps, right_gaps = diff_engine.build_gap_map(rows)

    # 左侧占位行
    base_phantoms = []
    for line_idx, count in left_gaps.items():
        pt = _gap_anchor(base, line_idx)
        base_phantoms.append(sublime.Phantom(
            sublime.Region(pt), _spacer_html(count), sublime.LAYOUT_BLOCK))
    sess.base_phantoms.update(base_phantoms)

    # 右侧占位行
    comp_phantoms = []
    for line_idx, count in right_gaps.items():
        pt = _gap_anchor(comp, line_idx)
        comp_phantoms.append(sublime.Phantom(
            sublime.Region(pt), _spacer_html(count), sublime.LAYOUT_BLOCK))
    sess.compare_phantoms.update(comp_phantoms)


# =============================================================================
# 会话开启 / 关闭
# =============================================================================
def _close_empty_views(window):
    """关闭窗口中的空白未命名视图。

    新窗口创建时会自动生成一个空白的 untitled 标签页，
    此函数用于清除这些多余的空白标签。

    参数:
        window: 目标窗口
    """
    for view in window.views():
        if (view.is_valid() and not view.file_name()
                and not view.name() and view.size() == 0):
            view.set_scratch(True)
            view.close()


def start_compare(original_window, base_view, compare_view):
    """发起一次并排比较，弹出独立新窗口进行。

    流程：
    1. 校验视图有效性
    2. 获取左右文件路径与内容
    3. 创建新窗口并设置两列布局
    4. 在新窗口中打开左侧文件(只读)和右侧文件(可编辑)
    5. 初始化 CompareSession 并执行首次渲染
    6. 启动联动滚动

    参数:
        original_window: 发起比较的原始窗口
        base_view:       左侧基准视图(在原始窗口中)
        compare_view:    右侧比较视图(在原始窗口中)
    """
    if base_view is None or compare_view is None:
        sublime.status_message(_t("msg.need_views"))
        return
    if base_view.id() == compare_view.id():
        sublime.status_message(_t("msg.need_different"))
        return

    # 获取文件路径
    base_path = base_view.file_name()
    compare_path = compare_view.file_name()

    # 获取文件内容（用于无文件路径的 scratch 视图）
    base_content = base_view.substr(sublime.Region(0, base_view.size()))
    compare_content = compare_view.substr(sublime.Region(0, compare_view.size()))
    base_name = base_view.name() or (
        os.path.basename(base_path) if base_path else _t("label.untitled"))
    compare_name = compare_view.name() or (
        os.path.basename(compare_path) if compare_path
        else _t("label.untitled"))

    # 获取语法设置（用于 scratch 视图继承语法高亮）
    base_syntax = base_view.settings().get("syntax")
    compare_syntax = compare_view.settings().get("syntax")

    # 创建新窗口
    sublime.run_command('new_window')
    compare_window = sublime.active_window()

    # 隐藏侧边栏和迷你地图，精简比较界面
    compare_window.set_sidebar_visible(False)
    compare_window.set_minimap_visible(False)

    # 关闭新窗口自带的空白 untitled 标签页
    _close_empty_views(compare_window)

    # 设置两列布局
    compare_window.set_layout(LAYOUT_TWO_COL)

    # 在新窗口中打开/创建左侧视图
    if base_path and os.path.isfile(base_path):
        # 有磁盘文件：直接打开
        new_base_view = compare_window.open_file(base_path)
        # 等待文件加载完成后再继续设置
        _wait_for_load(new_base_view, lambda: _finish_start(
            original_window, compare_window, new_base_view,
            compare_path, compare_name, compare_syntax, compare_content))
    else:
        # 无磁盘文件(scratch)：创建 scratch 视图并填充内容
        new_base_view = compare_window.new_file()
        new_base_view.set_scratch(True)
        new_base_view.set_name("[基准] " + base_name)
        new_base_view.run_command("append", {"characters": base_content})
        if base_syntax:
            new_base_view.assign_syntax(base_syntax)
        _finish_start(
            original_window, compare_window, new_base_view,
            compare_path, compare_name, compare_syntax, compare_content)


def _wait_for_load(view, callback):
    """等待视图加载完成后执行回调。

    Sublime Text 的 open_file 是异步的，文件可能尚未加载完成。
    此函数通过轮询 is_loading() 状态来等待加载完毕。

    参数:
        view:     等待加载的视图
        callback: 加载完成后执行的回调函数
    """
    if view.is_loading():
        sublime.set_timeout(lambda: _wait_for_load(view, callback), 50)
    else:
        callback()


def _finish_start(original_window, compare_window, new_base_view,
                  compare_path, compare_name, compare_syntax, compare_content):
    """完成比较启动的后半部分（在左侧视图加载完成后调用）。

    创建右侧视图、设置只读状态、注册会话、执行首次渲染。

    参数:
        original_window:  原始窗口
        compare_window:   比较窗口
        new_base_view:    新窗口中的左侧视图
        compare_path:     右侧文件路径
        compare_name:     右侧文件名
        compare_syntax:   右侧语法设置
        compare_content:  右侧文件内容
    """
    # 将左侧视图移到 group 0
    compare_window.set_view_index(new_base_view, 0, 0)

    # 在新窗口中打开/创建右侧视图
    if compare_path and os.path.isfile(compare_path):
        new_compare_view = compare_window.open_file(compare_path)
        _wait_for_load(new_compare_view, lambda: _finish_start_phase2(
            original_window, compare_window, new_base_view, new_compare_view))
    else:
        # 无磁盘文件(scratch)：创建可编辑 scratch 视图
        new_compare_view = compare_window.new_file()
        new_compare_view.set_name("[比较] " + compare_name)
        new_compare_view.run_command("append", {"characters": compare_content})
        if compare_syntax:
            new_compare_view.assign_syntax(compare_syntax)
        _finish_start_phase2(
            original_window, compare_window, new_base_view, new_compare_view)


def _finish_start_phase2(original_window, compare_window, new_base_view,
                         new_compare_view):
    """完成比较启动的最终阶段（在右侧视图也加载完成后调用）。

    设置视图属性、创建会话、执行渲染、启动滚动同步。

    参数:
        original_window:   原始窗口
        compare_window:    比较窗口
        new_base_view:     左侧视图
        new_compare_view:  右侧视图
    """
    cfg = get_config()

    # 将右侧视图移到 group 1
    compare_window.set_view_index(new_compare_view, 1, 0)

    # 结束该比较窗口中可能存在的旧会话
    old_sess = _sessions.pop(compare_window.id(), None)
    if old_sess:
        old_sess.active = False

    # 创建新会话
    sess = CompareSession(
        original_window=original_window,
        compare_window=compare_window,
        base_view=new_base_view,
        compare_view=new_compare_view,
    )
    _sessions[compare_window.id()] = sess

    # 左侧设置只读
    if cfg["base_readonly"]:
        new_base_view.set_read_only(True)

    # 状态栏标识
    new_base_view.set_status("compare_live", _t("status.base"))
    new_compare_view.set_status("compare_live", _t("status.compare"))

    # 首次渲染差异
    render_session(sess)

    # 聚焦右侧视图（可编辑侧）
    compare_window.focus_view(new_compare_view)

    # 启动联动滚动
    _schedule_scroll(sess)

    sublime.status_message(_t("msg.started"))


def _clear_view_marks(view, scratch=False):
    """清除单个视图上的高亮区域与状态栏标记。

    所有操作包裹 try/except，防止访问已销毁视图时闪退。

    参数:
        view:    目标视图
        scratch: 为 True 时将视图置为 scratch（跳过保存提示）
    """
    try:
        if view and view.is_valid():
            view.erase_regions(RK_LINE)
            view.erase_regions(RK_LINE_CHG)
            view.erase_regions(RK_CHAR)
            view.erase_status("compare_live")
            if scratch:
                view.set_scratch(True)
    except Exception:
        pass


def _clear_phantoms(sess):
    """安全清除会话左右两侧的 Phantom 占位行（视图可能已无效）。"""
    try:
        if sess.base_view and sess.base_view.is_valid():
            sess.base_phantoms.update([])
    except Exception:
        pass
    try:
        if sess.compare_view and sess.compare_view.is_valid():
            sess.compare_phantoms.update([])
    except Exception:
        pass


def _focus_window_later(window, delay):
    """延迟将指定窗口带到前台（等待窗口关闭动画完成）。"""
    def _do():
        try:
            if window and window.is_valid():
                window.bring_to_front()
        except Exception:
            pass

    sublime.set_timeout(_do, delay)


def end_session(compare_window, restore=True):
    """结束比较会话，清理资源并关闭比较窗口。

    清除高亮区域、幽灵占位行、状态栏标记，恢复只读状态，
    关闭比较窗口并将焦点回归原始窗口。
    内置防重入机制，避免多次调用导致闪退。

    参数:
        compare_window: 比较窗口
        restore:        是否关闭比较窗口并回归原始窗口
    """
    sess = _sessions.pop(compare_window.id(), None)
    if not sess:
        return
    # 防重入：如果已在关闭流程中则跳过
    if sess._closing:
        return
    sess._closing = True
    sess.active = False

    # 清除视图上的高亮和标记
    for view in (sess.base_view, sess.compare_view):
        _clear_view_marks(view)

    # 恢复左侧只读状态
    try:
        if sess.base_view and sess.base_view.is_valid():
            sess.base_view.set_read_only(sess.prev_readonly)
    except Exception:
        pass

    # 清除 Phantom 占位行
    _clear_phantoms(sess)

    if restore:
        original_window = sess.original_window

        # 延迟关闭比较窗口，避免在事件回调中同步执行 close_window 导致闪退
        def _do_close():
            try:
                if compare_window.is_valid():
                    for view in compare_window.views():
                        try:
                            if view.is_valid():
                                view.set_scratch(True)
                        except Exception:
                            pass
                    compare_window.run_command('close_window')
            except Exception:
                pass

            # 延迟聚焦原始窗口（等待窗口关闭动画完成）
            _focus_window_later(original_window, 150)

        # 使用 set_timeout 延迟执行关闭，脱离当前事件回调栈
        sublime.set_timeout(_do_close, 10)


# =============================================================================
# 联动滚动（轮询实现，Sublime 无原生滚动事件）
# =============================================================================
def _schedule_scroll(sess):
    """启动联动滚动的轮询定时器。

    按固定间隔周期性检测左右视图的滚动位置变化，
    当一侧发生用户滚动时，同步另一侧到对应位置。

    参数:
        sess: CompareSession 实例
    """
    def _tick():
        if not sess.active:
            return
        _scroll_tick(sess)
        sublime.set_timeout(_tick, SCROLL_INTERVAL_MS)

    sublime.set_timeout(_tick, SCROLL_INTERVAL_MS)


def _map_row(sess, row, src_is_left):
    """将源侧行号映射到目标侧对应行号。

    参数:
        sess:       CompareSession 实例
        row:        源侧行号
        src_is_left: True 表示源为左侧，False 表示源为右侧

    返回:
        int: 目标侧的对应行号
    """
    table = sess.left2right if src_is_left else sess.right2left
    if not table:
        return row
    if row < 0:
        row = 0
    if row >= len(table):
        row = len(table) - 1
    return table[row]


def _do_sync(sess, src, dst, src_is_left):
    """执行一次从源视图到目标视图的滚动同步。

    根据源视图当前可见区域的顶部行号，映射到目标侧对应行号，
    然后将目标视图滚动到该位置。

    参数:
        sess:        CompareSession 实例
        src:         源视图
        dst:         目标视图
        src_is_left: 源视图是否为左侧
    """
    y = src.viewport_position()[1]
    top_pt = src.layout_to_text((0, y))
    row = src.rowcol(top_pt)[0]
    drow = _map_row(sess, row, src_is_left)
    dpt = dst.text_point(drow, 0)
    dy = dst.text_to_layout(dpt)[1]
    dx = dst.viewport_position()[0]
    sess.syncing = True
    dst.set_viewport_position((dx, dy), False)


def _scroll_tick(sess):
    """联动滚动的单次轮询检测。

    检测左右视图的 viewport 位置变化，判断哪一侧发生了用户滚动，
    然后同步另一侧。包含去抖逻辑避免左右互相追逐。
    所有视图访问操作均包裹 try/except，防止视图销毁时闪退。

    参数:
        sess: CompareSession 实例
    """
    try:
        base, comp = sess.base_view, sess.compare_view
        if not (base.is_valid() and comp.is_valid()):
            return  # 视图无效时静默退出，由 on_pre_close 处理清理

        bvp = base.viewport_position()
        cvp = comp.viewport_position()

        # 吸收上一轮由程序触发的滚动，避免左右互相追逐
        if sess.syncing:
            sess.last_base_vp = bvp
            sess.last_comp_vp = cvp
            sess.syncing = False
            return

        thresh = 1.5
        base_changed = abs(bvp[1] - sess.last_base_vp[1]) > thresh
        comp_changed = abs(cvp[1] - sess.last_comp_vp[1]) > thresh

        src = None
        if base_changed and not comp_changed:
            src = "left"
        elif comp_changed and not base_changed:
            src = "right"
        elif base_changed and comp_changed:
            act = sess.compare_window.active_view()
            src = "left" if act == base else "right"

        if src == "left":
            _do_sync(sess, base, comp, True)
        elif src == "right":
            _do_sync(sess, comp, base, False)
        else:
            sess.last_base_vp = bvp
            sess.last_comp_vp = cvp
    except Exception:
        # 视图或窗口在滚动轮询期间被销毁，静默忽略
        pass


# =============================================================================
# 命令：比较来源选择
# =============================================================================
class CompareLiveCompareWithCommand(sublime_plugin.WindowCommand):
    """命令面板/菜单命令：选择任意另一个已打开标签与当前标签在新窗口中并排比较。

    弹出快速面板列出所有可选标签，用户选择后以被选标签为基准(左)，
    当前标签为比较(右)发起比较。
    """

    def run(self, group=-1, index=-1):
        """执行比较命令，弹出标签选择面板。"""
        origin = self._clicked_view(group, index) or self.window.active_view()
        others = [v for v in self.window.views() if v.id() != origin.id()]
        if not others:
            sublime.status_message(_t("msg.no_other_tabs"))
            return
        items = []
        for v in others:
            name = v.file_name() or v.name() or _t("label.untitled")
            items.append([name.split("\\")[-1].split("/")[-1], name])

        def on_done(i):
            if i < 0:
                return
            start_compare(self.window, base_view=others[i],
                          compare_view=origin)

        self.window.show_quick_panel(items, on_done)

    def _clicked_view(self, group, index):
        """根据 group/index 获取被点击的视图。"""
        if group is None or group < 0 or index is None or index < 0:
            return None
        views = self.window.views_in_group(group)
        if 0 <= index < len(views):
            return views[index]
        return None

    def is_visible(self, group=-1, index=-1):
        """至少有两个标签时才显示此菜单项。"""
        return len(self.window.views()) >= 2


# =============================================================================
# 命令：会话控制
# =============================================================================
class CompareLiveEndCommand(sublime_plugin.WindowCommand):
    """结束比较命令。

    关闭比较窗口并将焦点回归原始窗口。
    未保存的更改由 Sublime Text 系统原生保存提示处理。
    """

    def run(self):
        """执行结束比较命令。"""
        sess = session_for_window(self.window)
        if not sess:
            # 如果当前窗口不是比较窗口，尝试查找所有会话
            for s in _sessions.values():
                if s.active:
                    sess = s
                    break
        if not sess:
            sublime.status_message(_t("msg.no_session"))
            return

        compare_window = sess.compare_window
        end_session(compare_window, restore=True)
        sublime.status_message(_t("msg.ended"))

    def is_enabled(self):
        """存在活跃会话时可用。"""
        for s in _sessions.values():
            if s.active:
                return True
        return False


# =============================================================================
# 事件监听
# =============================================================================
class CompareLiveListener(sublime_plugin.EventListener):
    """CompareLive 全局事件监听器。

    监听以下事件：
    - on_modified: 右侧编辑时触发实时差异重算
    - on_pre_close_window: 比较窗口关闭时的数据保护兜底
    - on_pre_close: 视图关闭时的会话清理
    """

    def on_modified(self, view):
        """视图内容变更事件处理。

        当比较会话中的视图(尤其是右侧可编辑视图)发生修改时，
        触发去抖的差异重算，实现实时比较更新。

        参数:
            view: 发生修改的视图
        """
        sess = session_for_view(view)
        if sess:
            sess.schedule_rerender()

    def on_pre_close_window(self, window):
        """窗口关闭前事件处理。

        当用户通过窗口 X 按钮直接关闭比较窗口时，
        清理会话状态并聚焦原始窗口。
        未保存的更改由 Sublime Text 系统原生保存提示处理。

        参数:
            window: 即将关闭的窗口
        """
        sess = _sessions.get(window.id())
        if not sess or not sess.active:
            return
        # 防重入：如果 end_session 已在处理关闭则跳过
        if sess._closing:
            return
        sess._closing = True
        sess.active = False
        _sessions.pop(window.id(), None)

        # 清除视图标记并置为 scratch，避免关闭时重复弹出保存提示
        for view in (sess.base_view, sess.compare_view):
            _clear_view_marks(view, scratch=True)

        # 清除 Phantom
        _clear_phantoms(sess)

        # 延迟聚焦原始窗口
        _focus_window_later(sess.original_window, 200)

    def on_pre_close(self, view):
        """视图关闭前事件处理。

        当比较会话中的某个视图被单独关闭时（而非整个窗口关闭），
        结束对应的比较会话。增加防重入检查避免闪退。

        参数:
            view: 即将关闭的视图
        """
        sess = session_for_view(view)
        if sess and not sess._closing:
            compare_window = sess.compare_window
            # 延迟结束会话，避开关闭过程中的状态竞争
            sublime.set_timeout(
                lambda: end_session(compare_window, restore=True), 50)


# =============================================================================
# 菜单生成
# =============================================================================
def _regenerate_menus():
    """根据当前语言配置重新生成所有菜单和命令面板文件。"""
    pkg_dir = os.path.dirname(__file__)

    # Context.sublime-menu
    context_menu = [
        {"caption": "-", "id": "compare_live_sep_before"},
        {
            "caption": "CompareLive",
            "children": [
                {"caption": _t("menu.compare_with"), "command": "compare_live_compare_with"},
                {"caption": "-"},
                {"caption": _t("menu.end"), "command": "compare_live_end"},
            ],
        },
        {"caption": "-", "id": "compare_live_sep_after"},
    ]

    # Tab Context.sublime-menu
    tab_menu = [
        {"caption": "-", "id": "compare_live_tab_sep"},
        {"caption": "CompareLive", "command": "compare_live_compare_with"},
    ]

    # Main.sublime-menu
    main_menu = [
        {
            "id": "preferences",
            "children": [
                {
                    "id": "package-settings",
                    "caption": "Package Settings",
                    "children": [
                        {
                            "caption": "CompareLive",
                            "children": [
                                {
                                    "caption": "Settings",
                                    "command": "edit_settings",
                                    "args": {
                                        "base_file": "${packages}/CompareLive/CompareLive.sublime-settings",
                                        "default": "{\n\t$0\n}\n",
                                    },
                                },
                                {"caption": "-"},
                                {"caption": _t("menu.end_current"), "command": "compare_live_end"},
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "id": "tools",
            "children": [
                {
                    "caption": "CompareLive",
                    "children": [
                        {"caption": _t("menu.compare_with"), "command": "compare_live_compare_with"},
                        {"caption": "-"},
                        {"caption": _t("menu.end"), "command": "compare_live_end"},
                    ],
                }
            ],
        },
    ]

    # Default.sublime-commands
    commands = [
        {"caption": _t("cmd.compare_with"), "command": "compare_live_compare_with"},
        {"caption": _t("cmd.end"), "command": "compare_live_end"},
        {
            "caption": _t("cmd.settings"),
            "command": "edit_settings",
            "args": {
                "base_file": "${packages}/CompareLive/CompareLive.sublime-settings",
                "default": "{\n\t$0\n}\n",
            },
        },
    ]

    # 写入文件
    _write_json(os.path.join(pkg_dir, "Context.sublime-menu"), context_menu)
    _write_json(os.path.join(pkg_dir, "Tab Context.sublime-menu"), tab_menu)
    _write_json(os.path.join(pkg_dir, "Main.sublime-menu"), main_menu)
    _write_json(os.path.join(pkg_dir, "Default.sublime-commands"), commands)


def _write_json(path, data):
    """将数据序列化为 JSON 并写入文件。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")


# =============================================================================
# 生命周期
# =============================================================================
def _rerender_active_sessions(delay=300):
    """延迟刷新所有活跃会话的差异高亮（等待配色方案重载完成）。"""
    def _do():
        for sess in list(_sessions.values()):
            if sess.active:
                render_session(sess)

    sublime.set_timeout(_do, delay)


def _on_settings_changed():
    """设置文件变更回调。

    当用户修改 CompareLive.sublime-settings 时触发，
    强制重新生成配色方案 overlay 文件，重新生成菜单文件，
    并延迟刷新所有活跃会话的高亮。
    """
    cfg = get_config()
    # 强制写入，确保文件 mtime 变更以触发 Sublime Text 的文件监听重载
    color_scheme.generate(cfg["colors"], force=True)
    _regenerate_menus()
    _rerender_active_sessions()


def _on_color_scheme_changed():
    """用户全局 color scheme 切换回调。

    当用户切换 Preferences -> Color Scheme 时触发，
    需要重新生成与新 scheme 同名的 overlay 文件以确保合并生效。
    """
    cfg = get_config()
    color_scheme.generate(cfg["colors"], force=True)
    _rerender_active_sessions()


def plugin_loaded():
    """插件加载完成回调。

    在 Sublime Text 启动并加载本插件后执行：
    1. 生成与当前 color scheme 同名的 overlay 文件
    2. 根据语言配置生成菜单文件
    3. 注册设置变更监听（CompareLive 设置 + 全局 Preferences）
    """
    cfg = get_config()
    color_scheme.generate(cfg["colors"])
    _regenerate_menus()

    # 监听本插件设置变更
    s = sublime.load_settings(SETTINGS_FILE)
    s.clear_on_change("compare_live")
    s.add_on_change("compare_live", _on_settings_changed)

    # 监听用户全局 color scheme 切换（Preferences 中的 color_scheme 字段）
    prefs = sublime.load_settings("Preferences.sublime-settings")
    prefs.clear_on_change("compare_live_scheme")
    prefs.add_on_change("compare_live_scheme", _on_color_scheme_changed)


def plugin_unloaded():
    """插件卸载回调。

    在插件被卸载或 Sublime Text 关闭前执行：
    1. 移除设置变更监听
    2. 结束所有活跃的比较会话
    3. 清理生成的 overlay 文件
    """
    s = sublime.load_settings(SETTINGS_FILE)
    s.clear_on_change("compare_live")

    prefs = sublime.load_settings("Preferences.sublime-settings")
    prefs.clear_on_change("compare_live_scheme")

    # 清理 overlay 文件
    color_scheme.cleanup()

    for window_id in list(_sessions.keys()):
        sess = _sessions[window_id]
        if sess.active:
            sess.active = False
            # 清理但不关闭窗口（卸载时不应触发窗口关闭）
            for view in (sess.base_view, sess.compare_view):
                _clear_view_marks(view)
            _clear_phantoms(sess)
    _sessions.clear()
