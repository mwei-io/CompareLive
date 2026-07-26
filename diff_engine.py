import difflib
import re

# 对齐结果中每一行的标签常量
TAG_EQUAL = "equal"      # 两侧内容相同
TAG_REPLACE = "replace"  # 两侧均有内容但不同（修改）
TAG_DELETE = "delete"    # 仅左侧有内容（相对右侧被删除）
TAG_INSERT = "insert"    # 仅右侧有内容（相对左侧被新增）


def compute_alignment(left_lines, right_lines):
    """计算左右两组行的对齐映射表。

    使用 difflib.SequenceMatcher 进行严格字符序列匹配，将结果转换为逐行的
    对齐三元组列表，支持相等/替换/删除/插入四种状态。

    对于 replace 类型的操作码，将左右两侧按顺序逐行配对为“修改”，
    多出的行记录为删除(左多)或插入(右多)。

    参数:
        left_lines:  左侧文本行列表（每个元素为一行，不含换行符）
        right_lines: 右侧文本行列表

    返回:
        list[tuple]: 对齐映射表，元素为三元组 (left_index, right_index, tag)。
            left_index/right_index 为原始行下标(0-based)，若该侧无对应行则为 None。
            tag 取值为 TAG_EQUAL/TAG_REPLACE/TAG_DELETE/TAG_INSERT。
    """
    # 使用 SequenceMatcher 进行严格字符匹配
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines,
                                      autojunk=False)
    rows = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # 两侧完全相同的行：逐行配对
            for k in range(i2 - i1):
                rows.append((i1 + k, j1 + k, TAG_EQUAL))
        elif tag == "replace":
            # 两侧不同的区段：逐行配对为"修改"，多余部分为删除/插入
            left_span = i2 - i1
            right_span = j2 - j1
            paired = min(left_span, right_span)
            for k in range(paired):
                rows.append((i1 + k, j1 + k, TAG_REPLACE))
            for k in range(paired, left_span):
                rows.append((i1 + k, None, TAG_DELETE))
            for k in range(paired, right_span):
                rows.append((None, j1 + k, TAG_INSERT))
        elif tag == "delete":
            # 仅左侧有的行
            for k in range(i2 - i1):
                rows.append((i1 + k, None, TAG_DELETE))
        elif tag == "insert":
            # 仅右侧有的行
            for k in range(j2 - j1):
                rows.append((None, j1 + k, TAG_INSERT))
    return rows


def intraline_diff(left_text, right_text):
    """计算一对被修改行的行内字符级差异区间。

    对两行文本以"词/符号"为最小单元做差异分析（更贴近人眼感知），
    找出具体哪些字符/词发生了变化，用于行内差异高亮显示，
    区间坐标按字符偏移返回。

    参数:
        left_text:  左侧行文本
        right_text: 右侧行文本

    返回:
        tuple: (left_regions, right_regions)
            left_regions:  左侧需标注的字符区间列表 [(start, end), ...]
            right_regions: 右侧需标注的字符区间列表 [(start, end), ...]
            坐标为半开区间 [start, end)，基于各自行文本的字符下标。
    """
    # 词级匹配：先分词再比较，最终转回字符坐标
    left_tokens, left_map = _tokenize(left_text)
    right_tokens, right_map = _tokenize(right_text)
    matcher = difflib.SequenceMatcher(None, left_tokens, right_tokens,
                                      autojunk=False)
    left_regions, right_regions = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            left_regions.append((left_map[i1][0], left_map[i2 - 1][1]))
        if j2 > j1:
            right_regions.append((right_map[j1][0], right_map[j2 - 1][1]))
    return _merge_regions(left_regions), _merge_regions(right_regions)


# 分词正则：匹配连续字母数字、连续空白、或单个符号
_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _tokenize(text):
    """将文本切分为词元，并返回每个词元在原文中的字符偏移映射。

    用于行内差异的词级匹配：先按词元匹配找到差异区段，
    再通过映射表转换回原文的字符坐标。

    参数:
        text: 待分词的行文本

    返回:
        tuple: (tokens, spans)
            tokens: 词元字符串列表
            spans:  每个词元对应的 (start, end) 字符偏移元组列表
    """
    tokens = []
    spans = []
    for m in _TOKEN_RE.finditer(text):
        tokens.append(m.group(0))
        spans.append((m.start(), m.end()))
    return tokens, spans


def _merge_regions(regions):
    """合并相邻或重叠的字符区间，避免高亮碎片化。

    将排序后的区间列表中相邻（end >= 下一个 start）的区间合并为一个，
    使高亮显示更连贯、不闪烁。

    参数:
        regions: 字符区间列表 [(start, end), ...]，可能无序

    返回:
        list[tuple]: 合并后的有序区间列表 [(start, end), ...]
    """
    if not regions:
        return []
    regions = sorted(regions)
    merged = [list(regions[0])]
    for start, end in regions[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def build_gap_map(rows):
    """根据对齐表计算左右两侧需要插入的幽灵占位行信息。

    遍历对齐表，当一侧有行而另一侧无对应行时，累计缺失侧需要的占位行数。
    遇到两侧均有行的对齐行时，将累计的占位行数关联到该行的位置。

    参数:
        rows: 对齐映射表（compute_alignment 的返回值）

    返回:
        tuple: (left_gaps, right_gaps)
            每个 gaps 为 dict: { 后随实际行下标: 需在其前插入的空白行数 }
            使用哨兵键 -1 表示"文件末尾"需要追加的尾随占位行。
    """
    left_gaps = {}
    right_gaps = {}
    pending_left = 0   # 左侧待插入的占位行累计
    pending_right = 0  # 右侧待插入的占位行累计
    for left_idx, right_idx, tag in rows:
        if tag == TAG_INSERT:      # 右有左无 -> 左侧缺一行
            pending_left += 1
        elif tag == TAG_DELETE:    # 左有右无 -> 右侧缺一行
            pending_right += 1
        else:                       # equal / replace: 两侧均有实际行
            if pending_left and left_idx is not None:
                left_gaps[left_idx] = left_gaps.get(left_idx, 0) + pending_left
                pending_left = 0
            if pending_right and right_idx is not None:
                right_gaps[right_idx] = right_gaps.get(right_idx, 0) + pending_right
                pending_right = 0
    # 尾随占位行（差异在文件末尾）
    if pending_left:
        left_gaps[-1] = left_gaps.get(-1, 0) + pending_left
    if pending_right:
        right_gaps[-1] = right_gaps.get(-1, 0) + pending_right
    return left_gaps, right_gaps


def build_row_maps(rows, left_count, right_count):
    """构建左右行号双向映射数组，供联动滚动时行位置换算使用。

    为左侧每一行找到对应的右侧行号，为右侧每一行找到对应的左侧行号。
    无精确对齐的行取最近的前一个已知对齐行，保证结果单调不减。

    参数:
        rows:        对齐映射表
        left_count:  左侧总行数
        right_count: 右侧总行数

    返回:
        tuple: (left_to_right, right_to_left)
            left_to_right[i] = 与左侧第 i 行对齐的右侧行下标
            right_to_left[j] = 与右侧第 j 行对齐的左侧行下标
    """
    left_to_right = [0] * max(left_count, 1)
    right_to_left = [0] * max(right_count, 1)
    last_r = 0  # 最近一次已知对齐的右侧行号
    last_l = 0  # 最近一次已知对齐的左侧行号
    for left_idx, right_idx, _tag in rows:
        if left_idx is not None and right_idx is not None:
            last_l, last_r = left_idx, right_idx
        if left_idx is not None and left_idx < left_count:
            left_to_right[left_idx] = right_idx if right_idx is not None else last_r
        if right_idx is not None and right_idx < right_count:
            right_to_left[right_idx] = left_idx if left_idx is not None else last_l
    return left_to_right, right_to_left
