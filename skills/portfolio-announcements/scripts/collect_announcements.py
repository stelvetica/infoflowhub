#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拉取标的池 P0 公告：读标的池.json → 东财公告接口 → 类型白名单+标题关键词过滤 → 公告清单_YYYY-MM-DD.md

产物根固定为主 checkout 的 infoflowhub\\data\\reports（默认读 <产物根>\\标的池.json，
输出与留痕写 <产物根>）；D 盘观察目录由 infoflowhub 的 scripts/publish_reports.py 回写。

日期窗口（自上次运行以来）：A/H 两端均取闭区间 [last_run, 今日]。
盘后披露的公告 notice_date 存在「挂当日」与「挂次日」两种口径（实测各约半数），
左开区间会漏掉挂 last_run 当日的盘后公告（晨跑时尚未发布、次日跑又被区间排除），故取闭区间；
seen 留痕保证窗口拉长不重复输出。
last_run 记录在 used_announcements.json，缺省回看 3 天；window_start 记当日实际用的窗口起点，
当日重跑沿用它（last_run 已被推进到今日，沿用可避免丢掉挂昨日的行）。

A+H 公司（标的池 hk_code 字段）：A 股常发「自愿性公告」「H股公告」这类壳标题，无法从标题判类，
借港股同日描述性标题与港股类型判类；另补收港股独有实质类型（HK_ONLY_TYPES）。窄口径——港股
「海外監管公告-*」多为 A 股文件副本、「月報表」等为程序性噪音，均不收。
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

DEFAULT_POOL = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports\标的池.json")
DEFAULT_OUT_DIR = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
API = "https://np-anotice-stock.eastmoney.com/api/security/ann"

# 港股独有类型（A 股通道没有对应物）：仅对挂了 hk_code 的 A+H 标的补收
HK_ONLY_TYPES = {"其他-業務發展最新情況", "其他-營運業績最新情況", "盈利警告", "董事會召開日期"}

# P0 类型白名单（东财 columns[0].column_name 实测值；未枚举全，关键词兜底）
# 报告"摘要"与全文重复，不取
TYPE_WHITELIST = {
    "业绩预告", "业绩快报",
    "一季度报告全文", "半年度报告全文",
    "三季度报告全文", "年度报告全文",
    "分配预案", "分配方案实施", "分配方案决议公告",
    "股东/实际控制人股份减持", "权益变动报告书", "限售股份上市流通", "要约收购",
    "重大资产重组",
    # 港股（实测值；繁体）
    "中期/半年度報告", "中期業績", "全年業績",
} | HK_ONLY_TYPES

# 标题关键词：覆盖藏身「其他」的关键事件（简繁兼顾），命中即 P0
KEYWORDS = re.compile(
    r"立案|处罚|處罰|警示函|监管函|監管函|问询|問詢|诉讼|訴訟|仲裁|"
    r"重组|重組|收购|收購|出售|中标|中標|重大合同|停牌|退市|重整|减值|減值|"
    r"可转债|可轉債|可转换|可轉換|向特定对象发行|向特定對象發行|配股|增发|增發|配售|供股|"
    r"减持|減持|增持|承诺不减持|要约|要約|权益变动|權益變動|控制权变更|控制權變更|"
    r"利润分配|利潤分配|分红|分紅|派息|股息|"
    r"业绩预告|業績預告|业绩快报|業績快報|盈利警告|盈喜|盈警"
)

# 白名单/关键词命中但明确属噪音的：A股IPO超额配售（绿鞋）是发行机制文件，非再融资配售
EXCLUDE = re.compile(r"超额配售|超額配售|绿鞋|綠鞋")

# P2 类型噪音（《skill 口径》法律意见书一类程序性文件）：标题常含"分红/重组/要约"等 P0 关键词
# 而误命中，按类型整体排除；且此类行若留在组内会被 dedupe 当成关键件，误剔真正的分红实施公告
NOISE_TYPES = {"法律意见书"}

# A 股壳标题：不含实质信息（「药明康德:自愿性公告」「XX:H股公告」），判类时借港股同日描述性标题
GENERIC_TITLE = re.compile(
    r"^(?:自愿性公告|自願公告|自愿公告|公告|提示性公告|H股公告|海外监管公告|海外監管公告)$")


def bare_title(title):
    """去掉「药明康德:」这类公司名前缀，得到判壳用的标题"""
    return re.sub(r"^[^:：]{0,20}[:：]\s*", "", title).strip()


def is_generic_title(title):
    return bool(GENERIC_TITLE.match(bare_title(title)))

# 程序性会议决议（董事会/监事会/股东大会）：无增量信息，实质事项均另有专项公告；
# 标题含 P0 关键词的决议（如"利润分配…决议公告"）仍保留
RESOLUTION = re.compile(r"(董事会|监事会|股东(大)?会)[^。]{0,12}决议(公告)?$")

# 同一事件的衍生文件去重：财报/再融资/分红各成一组，组内只留关键公告，
# 提示性/论证/可行性/摊薄/审核意见/实施公告/过户登记等配套文件剔除
FIN = re.compile(r"可转债|可轉債|向特定对象发行|向特定對象發行|配股|增发|增發|供股|配售")
FIN_DERIV = re.compile(
    r"提示性|论证分析|論證分析|可行性|攤薄|摊薄|财务资助|財務資助|审核意见|審核意見|"
    r"回报规划|回報規劃|最近五年被证券监管|最近五年被證券監管|决议|決議")
DIV = re.compile(r"利润分配|利潤分配|分红|分紅|派息|股息|权益分派|權益分派")
DIV_KEY = re.compile(r"利润分配|利潤分配|分配预案|分配建議|建议派付|建議派付")
DIV_DERIV = re.compile(r"实施|實施|暂停办理|暫停辦理|过户|過戶|公告表格|决议|決議")
REP = re.compile(r"半年度报告|半年度報告|年度报告|年度報告|季度报告|中期報告|中期業績|全年業績")
REP_DERIV = re.compile(r"摘要|计提|計提|减值|減值|说明会|說明會|资金占用|資金佔用")


def dedupe(hits):
    """同标的同事件只留关键公告；组内无关键公告时衍生件也保留（窗口只抓到衍生件不丢信息）"""
    def has(code, main_re, deriv_re):
        return any(x["code"] == code and main_re.search(x["title"])
                   and not deriv_re.search(x["title"]) for x in hits)

    keep = []
    for h in hits:
        t, c = h["title"], h["code"]
        if REP_DERIV.search(t) and has(c, REP, REP_DERIV):
            continue
        # 融资衍生件：提示性/论证/可行性等含衍生词即剔；监管回顾/回报规划
        # 这类配套文件在同标的存在融资关键件时也剔
        if FIN_DERIV.search(t) and has(c, FIN, FIN_DERIV):
            continue
        if DIV.search(t) and DIV_DERIV.search(t) and not DIV_KEY.search(t) \
                and has(c, DIV, DIV_DERIV):
            continue
        keep.append(h)
    return keep


def to_em_code(code):
    """标的池代码 → (东财stock_list参数, ann_type)。002179.SZ→(002179,A)；3696.HK→(03696,H)"""
    num, _, mkt = code.partition(".")
    if mkt == "HK":
        return "%05d" % int(num), "H"
    return num, "A"


def is_p0(title, col_type):
    if EXCLUDE.search(title) or col_type in NOISE_TYPES:
        return False
    if RESOLUTION.search(title) and not KEYWORDS.search(title):
        return False
    return col_type in TYPE_WHITELIST or bool(KEYWORDS.search(title))


def in_window(notice_date, start, end):
    """自上次运行以来的窗口（闭区间）：notice_date 形如 2026-08-27"""
    return start <= notice_date[:10] <= end


def pick_window_start(used, today_iso, lookback_days):
    """窗口起点：当日重跑沿用当日已用起点（last_run 已推进到今日，
    否则挂昨日的行会被 [今日, 今日] 窗口剔掉）；跨日才用 last_run / 回看天数"""
    if used.get("last_run") == today_iso and used.get("window_start"):
        return used["window_start"]
    return used.get("last_run") or (date.fromisoformat(today_iso) - timedelta(days=lookback_days)).isoformat()


def unseen_today(hits, seen, today_iso):
    """当日新增：art 未出现在当日已输出的留痕里；为空即当日重跑无新增（保留已有清单不覆盖）"""
    today_seen = {a for a, d in seen.items() if d == today_iso}
    return [h for h in hits if h["art"] not in today_seen]


def collect_stock(stock, anns, h_anns, start, today_iso, seen):
    """单标的取数：A 通道判类 → 壳标题借港股同日描述性标题/类型 → 补收港股独有实质类型。

    hits 带 src_code（详情链接用）与 from_hk（展示标记）；同日 A 通道已出 P0 时港股不重复收。
    """
    hk_code = stock.get("hk_code")
    h_by_date = {}
    for h in h_anns:
        h_by_date.setdefault(h.get("notice_date", "")[:10], []).append(h)

    def fresh(art):
        """当日已输出的仍纳入（当日重跑不丢行），跨日的才去重跳过"""
        seen_date = seen.get(art)
        return not seen_date or seen_date == today_iso

    hits, used_h, day_p0 = [], set(), set()
    for a in anns:
        if not fresh(a.get("art_code", "")):
            continue
        if not in_window(a.get("notice_date", ""), start, today_iso):
            continue
        item, src = a, stock["code"]
        if hk_code and is_generic_title(a.get("title", "")):
            for h in h_by_date.get(a["notice_date"][:10], []):
                if not is_generic_title(h.get("title", "")):
                    item, src = h, hk_code
                    used_h.add(h.get("art_code", ""))
                    break
        title = item.get("title", "")
        col = (item.get("columns") or [{}])[0].get("column_name", "")
        if is_p0(title, col):
            hits.append({**stock, "date": item["notice_date"][:10], "type": col,
                         "title": title, "art": item.get("art_code", ""),
                         "src_code": src, "from_hk": src != stock["code"]})
            day_p0.add(item["notice_date"][:10])

    for h in h_anns:
        art = h.get("art_code", "")
        if art in used_h or not fresh(art):
            continue
        if not in_window(h.get("notice_date", ""), start, today_iso):
            continue
        date = h["notice_date"][:10]
        col = (h.get("columns") or [{}])[0].get("column_name", "")
        if col not in HK_ONLY_TYPES or date in day_p0:
            continue
        if is_p0(h.get("title", ""), col):
            hits.append({**stock, "date": date, "type": col,
                         "title": h.get("title", ""), "art": art,
                         "src_code": hk_code, "from_hk": True})
    return hits


def fetch_list(num, ann_type, page_size=100, retries=2):
    url = (f"{API}?sr=-1&page_size={page_size}&page_index=1&ann_type={ann_type}"
           f"&client_source=web&stock_list={num}&f_node=0&s_node=0")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)["data"]["list"]
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--selftest", action="store_true")
    # 窗口起点：留痕文件缺 last_run 时回看天数
    ap.add_argument("--lookback-days", type=int, default=3)
    args = ap.parse_args()

    if args.selftest:
        # 路径常量必须指向主 checkout 的 infoflowhub\data\reports（防回归到 D 盘失效路径）
        assert DEFAULT_OUT_DIR == Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
        assert DEFAULT_POOL == DEFAULT_OUT_DIR / "标的池.json"
        assert "infoflowhub-reports" not in str(DEFAULT_OUT_DIR)  # 产物根是主 checkout，不是 worktree
        assert to_em_code("3696.HK") == ("03696", "H")
        assert to_em_code("002179.SZ") == ("002179", "A")
        assert to_em_code("688291.SH") == ("688291", "A")
        assert is_p0("XX:2026年半年度报告", "半年度报告全文")
        assert not is_p0("XX:2026年半年度报告摘要", "半年度报告摘要")
        assert is_p0("XX:关于收到中国证监会立案通知的公告", "其他")
        assert is_p0("XX:发行可转换公司债券预案", "其他")
        assert not is_p0("XX:独立董事提名人声明与承诺", "独立董事提名人声明")
        assert not is_p0("XX:关于使用闲置募集资金进行现金管理的公告", "其他")
        assert not is_p0("XX:首次公开发行股票并在科创板上市超额配售选择权实施公告", "其他")
        # P2 类型噪音：标题含"分红"仍不取；且它被剔后，同组分红实施公告不得被 dedupe 误剔
        # （真实回归：澜起科技 2026-10-17 差异化分红法律意见书 ↔ 半年度权益分派实施公告）
        assert not is_p0("XX:关于差异化分红事项之专项法律意见书", "法律意见书")
        assert is_p0("XX:2026年半年度权益分派实施公告", "分配方案实施")
        assert not is_p0("XX:第七届董事会第二次会议决议公告", "分配预案")
        assert not is_p0("XX:2026年第三次临时股东大会决议公告", "其他")
        assert is_p0("XX:关于2026年中期利润分配预案的董事会决议公告", "分配预案")
        # 窗口闭区间：last_run 当日的盘后公告（notice_date 挂当日）必须纳入
        assert in_window("2026-08-28", "2026-08-28", "2026-08-31")
        assert in_window("2026-08-31", "2026-08-28", "2026-08-31")
        assert not in_window("2026-08-27", "2026-08-28", "2026-08-31")
        assert not in_window("2026-09-01", "2026-08-28", "2026-08-31")
        # 窗口起点：当日重跑沿用当日起点（不丢挂昨日的行），跨日才按 last_run/回看
        assert pick_window_start({"last_run": "2026-09-23", "window_start": "2026-09-22"},
                                 "2026-09-23", 3) == "2026-09-22"
        assert pick_window_start({"last_run": "2026-09-22"}, "2026-09-23", 3) == "2026-09-22"
        assert pick_window_start({}, "2026-09-23", 3) == "2026-09-20"
        # 关键词：出售资产与收购同属重大经营，须命中
        assert is_p0("XX:关于出售参股公司股权的公告", "收购出售资产/股权")
        # 同事件去重：定增配套文件剔除，留预案/募集说明书
        mk = lambda i, t, c="其他": {"code": "X", "title": t, "art": i, "col": c}
        # 当日重跑无新增（候选全为当日已输出）→ 判定为空，保留已有清单不覆盖
        assert unseen_today([mk("1", "X")], {"1": "2026-09-23"}, "2026-09-23") == []
        assert [h["art"] for h in unseen_today([mk("1", "X"), mk("3", "Z")],
                                               {"1": "2026-09-23"}, "2026-09-23")] == ["3"]
        grp = [mk("1", "XX:2026年度向特定对象发行A股股票预案"),
               mk("2", "XX:关于向特定对象发行股票募集说明书(注册稿)等相关文件更新的提示性公告"),
               mk("3", "XX:2026年度向特定对象发行A股股票方案论证分析报告"),
               mk("4", "XX:关于最近五年被证券监管部门和证券交易所采取监管措施或处罚及整改措施的公告"),
               mk("5", "XX:未来三年(2026—2028年)股东分红回报规划")]
        assert [h["art"] for h in dedupe(grp)] == ["1"]
        # 财报组：留报告全文，剔除计提减值
        grp2 = [mk("1", "XX:2026年半年度报告"), mk("2", "XX:关于2026年半年度计提资产减值准备的公告")]
        assert [h["art"] for h in dedupe(grp2)] == ["1"]
        # 分红组：留分配方案，剔除实施公告
        grp3 = [mk("1", "XX:关于2026年第二次中期利润分配方案公告"),
                mk("2", "XX:2026年第二次中期权益分派实施公告")]
        assert [h["art"] for h in dedupe(grp3)] == ["1"]
        # 组内无关键公告时衍生件保留
        grp4 = [mk("1", "XX:2026年第二次中期权益分派实施公告")]
        assert [h["art"] for h in dedupe(grp4)] == ["1"]
        # 端到端：法律意见书被 P2 类型剔后，同组分红实施公告存活（不误剔）
        raw5 = [mk("1", "XX:关于差异化分红事项之专项法律意见书", "法律意见书"),
                mk("2", "XX:2026年半年度权益分派实施公告", "分配方案实施")]
        assert [h["art"] for h in dedupe([h for h in raw5 if is_p0(h["title"], h["col"])])] == ["2"]
        # A+H 孪生：A 股壳标题借港股同日描述性标题/类型判类（药明康德 2026-11-10 真实回归）
        assert to_em_code("02359.HK") == ("02359", "H")
        assert is_generic_title("药明康德:自愿性公告")
        assert is_generic_title("药明康德:H股公告")
        assert is_generic_title("药明康德:海外监管公告")
        assert not is_generic_title("药明康德:关于收到中国证监会立案通知的公告")
        assert not is_generic_title("自愿公告 - 药明康德从1260H项下中国军工企业名单中移除")
        assert is_p0("董事会会议召开日期", "董事會召開日期")
        assert not is_p0("截至二零二六年十月三十一日止月份之股份发行人的证券变动月报表", "月報表")
        S = {"code": "603259.SH", "name": "药明康德", "holders": ["X"], "max_weight": 0.1,
             "hk_code": "02359.HK"}
        a1 = [{"art_code": "A1", "notice_date": "2026-11-10", "title": "药明康德:自愿性公告",
               "columns": [{"column_name": "其他"}]}]
        h1 = [{"art_code": "H1", "notice_date": "2026-11-10",
               "title": "自愿公告 - 药明康德从1260H项下中国军工企业名单中移除",
               "columns": [{"column_name": "其他-業務發展最新情況"}]}]
        r1 = collect_stock(S, a1, h1, "2026-11-09", "2026-11-10", {})
        assert len(r1) == 1 and r1[0]["art"] == "H1"
        assert r1[0]["from_hk"] and r1[0]["src_code"] == "02359.HK"
        # A 通道同日已出 P0 → 港股不重复收（澜起科技 2026-10-20 业绩预增：A 业绩预告 + H 盈利警告）
        S2 = {**S, "code": "688008.SH", "hk_code": "06809.HK"}
        a2 = [{"art_code": "A2", "notice_date": "2026-10-20",
               "title": "澜起科技:2026年前三季度业绩预增公告",
               "columns": [{"column_name": "业绩预告"}]}]
        h2 = [{"art_code": "H2", "notice_date": "2026-10-20",
               "title": "2026年第三季度业绩预增公告",
               "columns": [{"column_name": "盈利警告"}]}]
        assert [x["art"] for x in collect_stock(S2, a2, h2, "2026-10-19", "2026-10-20", {})] == ["A2"]
        # 港股独有实质类型且 A 股无同日项 → 收
        h3 = [{"art_code": "H3", "notice_date": "2026-10-12", "title": "董事会会议召开日期",
               "columns": [{"column_name": "董事會召開日期"}]}]
        assert [x["art"] for x in collect_stock(S, [], h3, "2026-10-10", "2026-10-12", {})] == ["H3"]
        # 港股噪音类型（月报表 / 海外监管公告副本）永不收
        h4 = [{"art_code": "H4", "notice_date": "2026-10-12",
               "title": "截至2026年9月30日止月份之股份发行人的证券变动月报表",
               "columns": [{"column_name": "月報表"}]},
              {"art_code": "H5", "notice_date": "2026-10-12",
               "title": "海外监管公告 - 关于回购A股股份的进展公告",
               "columns": [{"column_name": "海外監管公告-其他"}]}]
        assert collect_stock(S, [], h4, "2026-10-10", "2026-10-12", {}) == []
        # 无港股孪生：壳标题原样判类，不误收
        assert collect_stock({"code": "600233.SH", "name": "圆通速递", "holders": ["X"],
                              "max_weight": 0.01}, a1, [], "2026-11-09", "2026-11-10", {}) == []
        print("selftest PASS")
        return

    today_iso = date.today().isoformat()
    pool = json.loads(args.pool.read_text(encoding="utf-8"))
    used_path = args.out_dir / "used_announcements.json"
    used = json.loads(used_path.read_text(encoding="utf-8")) if used_path.exists() else {"seen": {}}
    start = pick_window_start(used, today_iso, args.lookback_days)

    hits, failures = [], []
    for s in pool["stocks"]:
        num, ann_type = to_em_code(s["code"])
        try:
            anns = fetch_list(num, ann_type)
        except Exception as e:
            failures.append(f"{s['code']} {type(e).__name__}: {e}")
            continue
        h_anns = []
        if s.get("hk_code"):
            try:
                h_anns = fetch_list(*to_em_code(s["hk_code"]))
            except Exception as e:
                # 港股通道失败不影响 A 股结果，但必须留痕
                failures.append(f"{s['hk_code']} {s['name']}（港股孪生）{type(e).__name__}: {e}")
        hits.extend(collect_stock(s, anns, h_anns, start, today_iso, used["seen"]))
        time.sleep(0.2)

    hits = dedupe(hits)
    hits.sort(key=lambda h: -h["max_weight"])
    out_path = args.out_dir / f"公告清单_{today_iso}.md"
    written = not (not unseen_today(hits, used["seen"], today_iso) and out_path.exists())
    if not written:
        print(f"今日无新增 P0，保留已有 {out_path}")
    else:
        lines = [f"# 公告清单 {today_iso}",
                 "",
                 f"> 数据源：东方财富 · 标的池 {pool['count']} 只 · 仅 P0 · "
                 f"公告日∈[上次运行, 今日] · 生成时间 {time.strftime('%H:%M')} · "
                 f"介绍列由 AI 联网补充（每条一句话）",
                 ""]
        cur = None
        for h in hits:
            if h["code"] != cur:
                if cur is not None:
                    lines.append("")
                cur = h["code"]
                lines.append(f"## {h['name']} {h['code']}（权重 {h['max_weight']:.1%} · "
                             f"{'/'.join(h['holders'])}）")
                lines.append("| 日期 | 类型 | 公告名 | 介绍 |")
                lines.append("|---|---|---|---|")
            detail = (f"https://data.eastmoney.com/notices/detail/"
                      f"{to_em_code(h['src_code'])[0]}/{h['art']}.html")
            title = h["title"].replace("|", "\\|")
            type_cell = h["type"] + ("（港股）" if h.get("from_hk") else "")
            lines.append(f"| {h['date']} | {type_cell} | [{title}]({detail}) | 待补 |")
        lines.append("")
        lines.append(f"共 {len(hits)} 条 P0，涉及 {len({h['code'] for h in hits})} 只标的。")
        if failures:
            lines.append("")
            lines.append("**未取到（需排查）**：")
            lines.extend(f"- {f}" for f in failures)
        out_path.write_text("\n".join(lines), encoding="utf-8")

    for h in hits:
        used["seen"][h["art"]] = today_iso
    used["last_run"] = today_iso
    used["window_start"] = start
    used_path.write_text(json.dumps(used, ensure_ascii=False, indent=1), encoding="utf-8")

    print((f"输出 {out_path}" if written else "未写文件"),
          f"P0 {len(hits)} 条 / {len({h['code'] for h in hits})} 只；失败 {len(failures)}")
    for f in failures:
        print("  " + f)


if __name__ == "__main__":
    main()
