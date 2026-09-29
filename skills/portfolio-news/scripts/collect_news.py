"""取各资讯源最新新闻 MD，解析热点条目并合并去重，输出单一热点清单。

职责（确定性）：
- 扫描各新闻源目录 *.md，逐源按「文件名日期（次键 mtime）」降序取最新 PER_SOURCE_LIMIT 条。
- 校验（全局口径，不按源分别要求）：所选集合中文件名起始日期 = 今日的条数 ≥ TODAY_MIN_COUNT；
  全部源目录都空/缺失则报错停止；单个源目录缺失或为空只跳过，并在输出中标注。
- 源格式自适应：含「单独一行纯数字」→ 蓝宝书编号条目模式；否则走文章模式（韭研公社等），
  按「段落块 + 标题行/正文块配对」切成多条热点（公社内容精选 / 学习笔记均按条分，不再整篇一条）。
- 剥离新闻包装头（大标题/发布时间/来源/原始页/章节说明/免责声明/作者利益披露）。
- 合并为单一热点清单：标题完全一致的热点合并（正文取更全版本），来源合并标注。
- 本次消费的文件名追加写入 used_news.json 留痕（只写不读，不参与过滤）。

不承担：跨标题语义去重、影响判定（由阶段三 AI 处理）。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

REPORTS_DIR = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
NEWS_DIRS = (
    Path(r"C:\Users\ETT\Playground\infoflowhub\data\alphapai_markdown"),
    Path(r"C:\Users\ETT\Playground\infoflowhub\data\jiuyangongshe_markdown"),
)
PER_SOURCE_LIMIT = 2
TODAY_MIN_COUNT = 1
USED_NEWS_NAME = "used_news.json"

# ponytail: 条目解析依赖蓝宝书固定格式（单独行数字开启条目、章节说明行固定措辞），
# 源格式变化时需同步调整 NOISE_PATTERNS / META_PREFIXES。
NOISE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^市场热点$",
        r"^机会前瞻$",
        r"^全球重点事件梳理$",
        r"^隔夜美股走势",
        r"^前日全球重点",
        r"^根据当前时段机构",
        r"^免责声明",
        r"^作者利益披露",
        r"^声明[:：]",
        r"^#",
        r"^-{3,}$",
    )
)
META_PREFIXES = ("- 发布时间:", "- 来源:", "- 原始页:")
SOURCE_TAG_RE = re.compile(r"(晚间版|晨会版|全球版|午间版|盘中版)")
SOURCE_LINE_RE = re.compile(r"^-\s*来源[:：]\s*(.+)$")

# ponytail: 韭研公社文章的「公众号固定模板语」硬编码过滤。这些句子是账号模板的一部分，
# 逐日稳定、几乎不变；一旦作者改版式，这里要同步调整（表现为分析里混入无意义热点）。
BOILERPLATE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^公社里海量内容不知从何看起",
        r"^我们结合公社热榜",
        r"^每日复盘必备",
        r"^今日具体文章如下",
        r"^【韭研AI】APP也已上线",
        r"^服务$",
        r"^基于公社实战理解的agent工具包",
        r"^聚焦公社内容和官方资讯的AI问答",
        r"^栏目入口$",
        r"^韭研AI[:：]实战派投研助手$",
        r"^一些行业/题材更新$",
        r"^一些个股逻辑[:：]?$",
        r"^新股研究[:：]?$",
        r"^\d+月\d+日\s*韭研AI.*点评",
        r"^风险提示[:：]",
        r"^本文为公社团队集体出品",
        r"^【韭研AI】",
        r"^内容来自韭研AI",
        r"^https?://\S+$",
    )
)
# 尾部标记：命中即丢弃其后全部内容（题材图表名、传送门链接等，无分析价值）。
# 只按块首匹配——正文里出现同名字样不应误截断。
ARTICLE_TAIL_MARKERS = ("部分题材图表如下", "公社内容传送门")
# 纯分组标签（如「一些个股逻辑：」「一、板块热议：」）——仅「很短且以冒号结尾」才算。
# 阈值取 12：真实标签实测 4~8 字（板块热议/新股研究/公社内容传送门/APP端…），
# 而「…需关注的环节如下：」这类 19~20 字的真正文句必须保住，不能当标签整块丢掉。
# 章节标记（一、二、三、…）实测全部带冒号，由本规则覆盖；**刻意不加** `^一、` 这类独立规则——
# 它会把「一、二线厂商都在提价」这类正文行一起吃掉，宁可多留一条正文也不误删。
ARTICLE_LABEL_MAX_LEN = 12
# 编号要点行（1、 / 2. / （3））永远是上一条的正文续写，不新开条目。
# 分隔点后若紧跟数字则不算编号（挡住 1.6T / 11.11 / 3.5亿元 这类小数与版本号开头）
ARTICLE_POINT_RE = re.compile(r"^(?:\d{1,2}[、)）]|[（(]\d{1,2}[)）]|\d{1,2}[.．](?!\d))")
ARTICLE_TITLE_MAX_LEN = 40  # 含冒号的标题行长度上限
ARTICLE_SHORT_LEN = 20  # 不含冒号时按标题行处理的上限
ARTICLE_TITLE_OUT_MAX = 60  # 长段落块派生标题的截断长度
# 只有「足够长的段落块」才回填为前一条标题的正文；否则它本身就是一条（券商标题等）
ARTICLE_BODY_MIN_LEN = 60
MAX_SCAN_PER_DIR = 20  # 每个源目录最多向前看几个文件来识别来源标签（每标签取 PER_SOURCE_LIMIT 条）


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="取各资讯源最新新闻，解析热点条目并合并去重")
    p.add_argument(
        "-d", "--dir", type=Path, action="append", default=None,
        help="新闻 MD 目录，可重复指定；默认两个资讯源目录（蓝宝书 / 韭研公社）",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=REPORTS_DIR / "今日新闻.md",
        help="输出 MD 路径",
    )
    p.add_argument(
        "--selftest", action="store_true",
        help="只跑内置夹具自检（离线），校验两类韭研文章的切条规则",
    )
    args = p.parse_args()
    # append + 可变默认值的坑：默认值不能直接挂在 action="append" 上（会被就地追加污染）
    args.dir = list(args.dir) if args.dir else list(NEWS_DIRS)
    return args


def extract_date_from_name(name: str) -> date | None:
    """文件名形如 '2026-07-07_国内蓝宝书...'，取前 10 位日期。"""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def is_bluebook_format(text: str) -> bool:
    """含单独一行纯数字 → 蓝宝书编号条目模式；否则文章模式（韭研公社，按条切分）。"""
    return any(re.fullmatch(r"\d+", line.strip()) for line in text.splitlines())


def source_label(path: Path, text: str) -> str:
    """来源标签：文件名版本词 > md 内 `- 来源:` 行末段 > 目录名兜底。

    废弃按文件名猜来源（对韭研这类「标题即文件名」的源会返回整串标题）。
    """
    m = SOURCE_TAG_RE.search(path.name)
    if m:
        return m.group(1)
    for raw in text.splitlines():
        m = SOURCE_LINE_RE.match(raw.strip())
        if m:
            seg = m.group(1).split()
            if seg:
                return seg[-1]
    return path.parent.name


def source_key(path: Path, text: str) -> str:
    """配额分组键 = md 头 `- 来源:` 整行值（`Alpha派 蓝宝书` / `韭研公社 韭菜团子` …）。

    与 `source_label`（展示用短标签，蓝宝书取版本词）区分：配额必须按「账号」分组，
    韭研公社目录下同时住着韭菜团子与韭韭八十一，按目录配额会让一个作者把另一个挤掉。
    """
    for raw in text.splitlines():
        m = SOURCE_LINE_RE.match(raw.strip())
        if m:
            return m.group(1).strip()
    return path.parent.name


def _head_text(path: Path, limit: int = 2048) -> str:
    """只读文件头部（够取 `- 来源:` 行即可），避免为判来源整读整个源目录。"""
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def collect_latest_news(dirs: list[Path], today: date) -> list[Path]:
    """逐「来源」取最新 PER_SOURCE_LIMIT 条，并做全局今日覆盖校验。"""
    latest: list[Path] = []
    for d in dirs:
        files = sorted(
            d.glob("*.md"),
            # ponytail: 主键用文件名日期而非 mtime——同步工具重落盘旧文件会把
            # mtime 全刷新，导致旧新闻挤掉今日文件；发布日期才是可靠信号。
            key=lambda f: (extract_date_from_name(f.name) or date.min, f.stat().st_mtime),
            reverse=True,
        )
        per_key: dict[str, int] = {}
        for f in files[:MAX_SCAN_PER_DIR]:
            key = source_key(f, _head_text(f))
            if per_key.get(key, 0) >= PER_SOURCE_LIMIT:
                continue
            per_key[key] = per_key.get(key, 0) + 1
            latest.append(f)
    if not latest:
        raise FileNotFoundError(
            "所有新闻源目录均为空或缺失，停止流程：\n"
            + "\n".join(str(d) for d in dirs)
        )

    today_count = sum(1 for f in latest if extract_date_from_name(f.name) == today)
    if today_count < TODAY_MIN_COUNT:
        names = [f.name[:80] for f in latest]
        raise ValueError(
            f"所选 {len(latest)} 条中仅 {today_count} 条为今日（{today}），"
            f"不满足≥{TODAY_MIN_COUNT} 条要求，停止流程。所选：\n"
            + "\n".join(names)
        )
    return latest


def skipped_sources(dirs: list[Path]) -> list[Path]:
    """缺失或为空的源目录（跳过不阻塞流程，在输出中标注）。"""
    return [d for d in dirs if not any(d.glob("*.md"))]


def is_noise(line: str) -> bool:
    """包装行判定：章节标题/说明/免责声明/新闻头。"""
    return any(p.search(line) for p in NOISE_PATTERNS) or line.startswith(META_PREFIXES)


def parse_entries(text: str) -> list[tuple[str, str]]:
    """蓝宝书模式：解析热点条目，返回 (标题, 正文) 列表。

    条目由单独一行的纯数字开启，其后首个非噪声行为标题，其余行为正文；
    噪声行（章节标题/说明/免责声明/新闻头）触发当前条目收尾并被丢弃。
    """
    entries: list[tuple[str, str]] = []
    pending_num = False
    title: str | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal title, body
        if title is not None:
            entries.append((title, "\n".join(body).strip()))
        title, body = None, []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_noise(line):
            flush()
            continue
        if re.fullmatch(r"\d+", line):
            flush()
            pending_num = True
            continue
        if pending_num:
            title, body = line, []
            pending_num = False
            continue
        if title is not None:
            body.append(line)
    flush()
    return entries


def _collapse(block: str) -> str:
    """段落块压成单行（判定用；写正文时仍保留原始换行）。"""
    return " ".join(part.strip() for part in block.splitlines() if part.strip())


# 韭研正文（公社内容精选）自带 markdown 链接：`[文章标题](原文地址)`
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _split_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """拆出 markdown 链接：返回 (去掉链接语法的纯文本, [(标题, 地址), ...])。

    链接只用于判定与「原文」行，不进标题/正文文本——否则标题会带上 URL、长度判定失真。
    """
    links = [(m.group(1).strip(), m.group(2).strip()) for m in _MD_LINK_RE.finditer(text)]
    return _MD_LINK_RE.sub(lambda m: m.group(1), text), links


def _is_title_block(block: str) -> bool:
    """单行、长度受限、不以句号收尾 → 视为「标题行」，其后块是它的正文。

    公社内容精选的每一条本身就是标题；学习笔记的「名称：题材」是标题行 + 正文块。
    """
    if len(block.splitlines()) > 1 or block.endswith("。"):
        return False
    if len(block) > ARTICLE_TITLE_MAX_LEN:
        return False
    if re.search(r"[:：]", block):
        return True
    return len(block) <= ARTICLE_SHORT_LEN


def _block_title(block: str) -> str:
    """长段落块派生标题：取首个句末标点前的内容，超长截断（截断补省略号）。"""
    head = re.split(r"[。！？]", block, maxsplit=1)[0].strip() or block
    if len(head) <= ARTICLE_TITLE_OUT_MAX:
        return head
    return head[:ARTICLE_TITLE_OUT_MAX] + "…"


def split_article_body(text: str) -> list[str]:
    """取 md 头（`# 标题` + 三行 meta）之后的正文，按空行切成段落块。

    meta 按「紧跟标题的连续区」跳过，不要求三行齐全——缺行时不致把 meta 混进条目。
    """
    lines = text.splitlines()
    body_start = 0
    for i, raw in enumerate(lines):
        if raw.strip().startswith("# "):
            body_start = i + 1
            break
    while body_start < len(lines):
        line = lines[body_start].strip()
        if line and not line.startswith(META_PREFIXES):
            break
        body_start += 1
    blocks: list[str] = []
    for chunk in re.split(r"\n\s*\n", "\n".join(lines[body_start:])):
        block = chunk.strip()
        if block:
            blocks.append(block)
    return blocks


def parse_article(text: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """文章模式（韭研公社等）：按段落块切条，标题行与正文块配对。

    - 公社内容精选 = 一串带原文链接的文章标题，各块自成一条（链接收进「原文」）；
    - 学习笔记 = 章节标记 +「名称：题材」标题行与其正文块配对；
    - 编号要点行（1、/ 2. /（3））一律并入上一条，不新开条目；
    - 公众号模板语、章节标记、短分组标签（如「一些个股逻辑：」）与尾部标记之后的内容全部丢弃。

    返回 `(标题, 正文, [(链接标题, 链接地址), ...])`。
    """
    items: list[list] = []  # [标题, 正文, 链接列表]

    def add(title: str, body: str, links: list[tuple[str, str]]) -> None:
        items.append([title, body, links])

    for block in split_article_body(text):
        flat = _collapse(block)
        if not flat:
            continue
        # 先剥链接语法再判定：模板语/尾部标记可能整行就是一个 markdown 链接
        plain, links = _split_links(flat)
        body_text = _MD_LINK_RE.sub(lambda m: m.group(1), block)
        if plain.startswith(ARTICLE_TAIL_MARKERS):
            break
        if any(p.search(plain) for p in BOILERPLATE_PATTERNS):
            continue
        if len(plain) <= ARTICLE_LABEL_MAX_LEN and plain.endswith((":", "：")):
            continue
        if ARTICLE_POINT_RE.match(plain):
            if items:
                items[-1][1] = f"{items[-1][1]}\n{body_text}".strip()
                items[-1][2].extend(links)
            else:
                # 首块就是编号要点：标题去掉编号前缀，正文保留原块
                add(_block_title(ARTICLE_POINT_RE.sub("", plain)), body_text, links)
            continue
        if _is_title_block(plain):
            add(plain, "", links)
            continue
        if items and not items[-1][1] and len(plain) >= ARTICLE_BODY_MIN_LEN:
            # 上一条是标题行、尚无正文，且本块足够长（真段落而非另一条标题）→ 回填为正文
            items[-1][1] = body_text
            items[-1][2].extend(links)
            continue
        # 长块自成一条：整块就是标题时不再把同一段重复进正文
        derived = _block_title(plain)
        add(derived, "" if derived == plain else body_text, links)
    return [(title, body, links) for title, body, links in items]


def parse_news(text: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """按源格式分发：蓝宝书编号条目 / 韭研文章按条切分。"""
    if is_bluebook_format(text):
        return [(title, body, []) for title, body in parse_entries(text)]
    return parse_article(text)


def merge_entries(
    all_entries: list[tuple[str, str, str, list[tuple[str, str]]]],
) -> tuple[list[dict], int]:
    """按标题精确去重合并；返回 (合并后条目, 去重条数)。

    正文保留信息更全（更长）的版本，来源合并标注，原文链接按出现顺序去重收集，
    顺序按首次出现。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for src, title, body, links in all_entries:
        key = title.strip()
        if key in merged:
            e = merged[key]
            if src not in e["sources"]:
                e["sources"].append(src)
            if len(body) > len(e["body"]):
                e["body"] = body
        else:
            merged[key] = {"title": title, "body": body, "sources": [src], "links": []}
            order.append(key)
        e = merged[key]
        for link in links:
            if link not in e["links"]:
                e["links"].append(link)
    dup = len(all_entries) - len(order)
    return [merged[k] for k in order], dup


def build_output(entries: list[dict], labels: list[str], skipped: list[Path],
                 today: date) -> str:
    lines: list[str] = [f"# 今日热点 {today}", ""]
    src_names = list(dict.fromkeys(labels))  # 同名源去重，保留出现顺序
    lines.append(f"> 合并自 {len(labels)} 条新闻：{' / '.join(src_names)}")
    if skipped:
        lines.append("")
        lines.append("> 已跳过（源目录缺失或为空）：" + "、".join(str(d) for d in skipped))
    lines.append("")
    lines.append("## 热点索引")
    lines.append("")
    lines.append("| # | 热点 | 来源 |")
    lines.append("|---|---|---|")
    for i, e in enumerate(entries, start=1):
        lines.append(f"| {i} | {e['title']} | {'+'.join(e['sources'])} |")
    lines.append("")

    for i, e in enumerate(entries, start=1):
        lines.append(f"## 热点 {i}：{e['title']}")
        lines.append("")
        lines.append(f"- 来源：{'、'.join(e['sources'])}")
        for label, url in e["links"]:
            lines.append(f"- 原文：[{label}]({url})")
        lines.append("")
        lines.append(e["body"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def record_used(path: Path, latest: list[Path]) -> None:
    """本次消费的文件名追加留痕（读取仅用于续写历史，不参与任何过滤/判断）。"""
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (OSError, json.JSONDecodeError):
        history = []
    history.extend(f.name for f in latest)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")


SELFTEST_PICKS = """# 9月21日 公社内容精选

- 发布时间: 2026/09/21 17:55
- 来源: 韭研公社 韭菜团子
- 原始页: https://www.jiuyangongshe.com/a/picks

公社里海量内容不知从何看起，看每天的公社每日内容精选就够了！

栏目入口

韭研AI：实战派投研助手

MLCC板块更新

⛵️【财通电子&新科技】MLCC板块更新

[MLCC涨价扩围，需求向AI迁移](https://www.jiuyangongshe.com/a/11dcltlchun)

[韭研AI：实战派投研助手](https://ai.jiuyangongshe.com/)

一些个股逻辑：

【长江汽车&电新】福赛科技：轻量化新料号持续增加，坚定看好轻

中塑股份（301686）：改性工程塑料

1、主营改性工程塑料研发、生产、销售，产品含改性PC、PC/ABS等。

2、国内消费电子改性工程塑料核心供应商，部分产品对标国际品牌。

【国金计算机】海内外算租延续涨价趋势，算力仍是稀缺资产，继续强call【算力租赁】

部分题材图表如下，更多题材图表可前往网站首页【产业库】查看

华字辈

公社内容传送门:

韭研公社全天涨停简图（09.21)
"""

SELFTEST_NOTE = """# 韭研公社学习笔记（260921）

- 发布时间: 2026/09/21 16:11
- 来源: 韭研公社 韭韭八十一
- 原始页: https://www.jiuyangongshe.com/a/note

一、板块热议：

医药医疗：工信部等十部门印发《医药工业发展 “十五五” 规划》，AI 制药、创新药政策预期升温，叠加流感相关题材催化板块走强。（南华生物、瑞康医药等）

房地产：国家统计局披露 1-8 月二手房网签面积同比增长 10.6%，二手房交易规模反超新房，存量地产服务方向受资金追捧。（世联行、绿地控股等）

风险提示：以上内容来自公社用户写作，相关信息未经核实，未必准确，请审慎参考。

二、大涨股解析：

近岸蛋白：AI制药

2026年9月18日盘后网传消息（未证实），美国财政部正在草拟对华药企投资新规，拟放行中国新药授权交易。公司核心产品为靶点及因子类蛋白，可为生物药企业提供定制化 CRO 服务。

新华制药：阿尔茨海默

2026年9月20日公告，全资子公司新达制药收到国家药监局核准签发的盐酸多奈哌齐口崩片《药品注册证书》，该药品用于轻度、中度或重度阿尔茨海默病症状的治疗，属于医保乙类品种。

三、新股研究：

中塑股份（301686）：改性工程塑料

1、主营改性工程塑料研发、生产、销售，产品含改性PC、PC/ABS等。

2、国内消费电子改性工程塑料核心供应商，部分产品对标国际品牌。

世纪数码（920229）：数码喷印设备

1、深耕工业数码喷印设备，聚焦纺织数码印花。

本文为公社团队集体出品，所有内容均来源于网络公开资料，仅做信息分享，无任何推荐之意，据此交易，风险自负。
"""

SELFTEST_EDGE = """# 边界用例

- 发布时间: 2026/09/21 10:00
- 来源: 韭研公社 韭菜团子
- 原始页: https://www.jiuyangongshe.com/a/edge

【韭研AI】

内容来自韭研AI

https://ai.jiuyangongshe.com/

MLCC板块更新

本轮涨价扩散至全渠道，需关注的环节如下：

某券商观点：部分题材图表如下所示，供参考。

三环集团公告提价30%。

一、二线厂商都在提价

1.6T光模块核心梳理站上光博会C位

[【韭研AI】](https://ai.jiuyangongshe.com/)

[部分题材图表如下](https://www.jiuyangongshe.com/a/tail)

这部分内容在链接式尾部标记之后，不应出现。
"""

SELFTEST_META = """# 缺 meta 行

- 发布时间: 2026/09/21 09:00

真实内容第一段足够长，用于验证 meta 缺行时不会泄漏成热点条目，这段话需要超过二十个字符。
"""


def run_selftest() -> int:
    """内置夹具自检：两类韭研文章的切条规则（离线，不读任何外部文件）。"""
    picks = parse_article(SELFTEST_PICKS)
    note = parse_article(SELFTEST_NOTE)
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))

    picks_titles = [t for t, _, _ in picks]
    check("精选：切出 6 条", len(picks) == 6)
    check("精选：首条为 MLCC板块更新", picks_titles[:1] == ["MLCC板块更新"])
    check("精选：模板语已剔除", not any("公社里海量" in t or "栏目入口" in t for t in picks_titles))
    check("精选：纯分组标签已剔除", not any(t.startswith("一些个股逻辑") for t in picks_titles))
    check("精选：尾部标记后内容已丢弃", not any("华字辈" in t or "传送门" in t for t in picks_titles))
    xin_gu = next((b for t, b, _ in picks if t.startswith("中塑股份")), "")
    check("精选：编号要点并入上一条", "1、" in xin_gu and "2、" in xin_gu)
    suan_zu = next((b for t, b, _ in picks if "算力租赁" in t), None)
    check("精选：整块即标题时不重复进正文", suan_zu == "")

    note_titles = [t for t, _, _ in note]
    check("学习笔记：切出 6 条", len(note) == 6)
    check("学习笔记：章节标记已剔除", not any(re.match(r"^[一二三四五六七八九十]+、", t) for t in note_titles))
    check("学习笔记：风险提示已剔除", not any("风险提示" in t for t in note_titles))
    check("学习笔记：结尾免责段已剔除", not any("公社团队集体出品" in t for t in note_titles))
    body = {t: b for t, b, _ in note}
    check("学习笔记：板块热议块自成一条且有正文", "医药医疗" in note_titles[0] and "南华生物" in body.get(note_titles[0], ""))
    check("学习笔记：长块标题超长时补省略号", note_titles[0].endswith("…"))
    check("学习笔记：标题行与其正文块配对", "美国财政部" in body.get("近岸蛋白：AI制药", ""))
    points = body.get("中塑股份（301686）：改性工程塑料", "")
    check("学习笔记：新股要点并入上一条", "1、" in points and "2、" in points)

    # 原文链接：标题不带链接语法、链接收进 links、正文不留链接语法
    picks_links = {t: links for t, _, links in picks}
    mlcc_links = picks_links.get("MLCC涨价扩围，需求向AI迁移", [])
    check("精选：原文链接已提取", len(mlcc_links) == 1 and mlcc_links[0][1].startswith("https://"))
    check("精选：标题不含链接语法", not any("](" in t for t in picks_titles))
    check("精选：正文不含链接语法", not any("](" in b for _, b, _ in picks))
    check("精选：无链接条目 links 为空", picks_links.get("MLCC板块更新", None) == [])
    check("学习笔记：无链接条目 links 为空", all(not links for _, _, links in note))

    # 边界用例：运营行过滤 / 冒号结尾正文块不丢 / 尾部标记只在块首匹配 / 以「一、」开头的正文不被当章节标记
    edge = parse_article(SELFTEST_EDGE)
    edge_text = "\n".join(f"{t}\n{b}" for t, b, _ in edge)
    edge_titles = [t for t, _, _ in edge]
    check("边界：运营行已过滤", not any("韭研AI" in t or "jiuyangongshe.com" in t for t in edge_titles))
    check("边界：20 字冒号结尾的正文块未被当标签丢弃", any(t.startswith("本轮涨价扩散至全渠道") for t in edge_titles))
    check("边界：小数开头的块未被当编号要点粘连", any(t.startswith("1.6T") for t in edge_titles))
    check("边界：尾部标记只在块首匹配（正文同名字样不误截断）", any("某券商观点" in t for t in edge_titles))
    check("边界：尾部标记之后的真内容仍在", "三环集团" in edge_text)
    check("边界：以「一、」开头的正文不被当章节标记丢弃", "一、二线厂商都在提价" in edge_text)
    check("边界：链接式模板语也被剔除", not any("实战派投研助手" in t or "【韭研AI】" in t for t in edge_titles))
    check("边界：链接式尾部标记也截断", "不应出现" not in edge_text)

    meta = parse_article(SELFTEST_META)
    check("边界：meta 缺行时不泄漏成条目", len(meta) == 1 and "发布时间" not in meta[0][0])

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"精选条数 {len(picks)} | 学习笔记条数 {len(note)} | 边界条数 {len(edge)} | 缺 meta 条数 {len(meta)}")
    if failed:
        print(f"selftest FAIL（{len(failed)} 项）：{'；'.join(failed)}")
        return 1
    print("selftest PASS")
    return 0


def main() -> None:
    args = parse_args()
    if args.selftest:
        raise SystemExit(run_selftest())
    today = date.today()
    latest = collect_latest_news(args.dir, today)
    skipped = skipped_sources(args.dir)

    all_entries: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    labels: list[str] = []
    for f in latest:
        text = f.read_text(encoding="utf-8")
        src = source_label(f, text)
        labels.append(src)
        for title, body, links in parse_news(text):
            all_entries.append((src, title, body, links))
    if not all_entries:
        raise ValueError(
            f"未能从 {len(latest)} 条新闻中解析出热点条目，停止流程（源格式可能变化）。"
        )

    entries, dup = merge_entries(all_entries)
    md = build_output(entries, labels, skipped, today)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    used_path = args.output.parent / USED_NEWS_NAME
    record_used(used_path, latest)

    print(
        f"已生成：{args.output.resolve()}"
        f"（{len(latest)} 条新闻 / {len(entries)} 个热点，跨新闻标题去重合并 {dup} 条；"
        f"留痕 {used_path.resolve()}）"
    )
    for d in skipped:
        print(f"  已跳过（源目录缺失或为空）：{d}")


if __name__ == "__main__":
    main()