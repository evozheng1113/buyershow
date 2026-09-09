#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小红书种草 · 文案 + 标签生成(从 CC 项目 writer.py / writer5.py 移植风格与规则)。

- 标签:规则生成(品牌词矩阵 + 黑话 + 分品类品类词/情绪词),精准、不花 API。
- 标题+正文:交给 GPT 生成,把「碎碎念 / 清冷质感」两套口吻的规则写进提示词。
  口吻依据 CC 拆解:清冷=平静描述质感、只 1 个 emoji、先承认平淡再说好、说"耐看"不说"绝了";
  碎碎念=人设+纠结时长+戴了多久+具体生活细节+结尾问句。两者混发,账号池才不像一个人写的。
- 硬性:必带该款「短板」(承认缺点才真实)、用到规格/材质/工艺/卖点、正文尽量 180+ 字。
"""
import random
import re

# ============================ 标签规则(移植 writer.py) ============================
# 品类词按 cat 分开,避免 #钻石手链 串进项链笔记。
品类词 = {
    "项链": ["#钻石项链", "#钻石项链款式", "#18K金项链", "#钻石吊坠", "#锁骨链", "#轻珠宝"],
    "手链": ["#钻石手链", "#钻石手链款式", "#18K金手链", "#轻珠宝", "#叠戴"],
    "耳饰": ["#钻石耳钉", "#钻石耳扣", "#18K金耳钉", "#轻珠宝", "#耳饰分享"],
    "戒指": ["#钻石戒指", "#钻戒", "#18K金戒指", "#轻珠宝", "#戒指分享"],
}
# 类型归一(填吊坠按项链走标签,手镯按手链走标签)
_CAT_NORM = {"吊坠": "项链", "手镯": "手链", "耳钉": "耳饰", "耳环": "耳饰", "耳钉/耳环": "耳饰"}
通用词 = ["#培育钻", "#钻石", "#培育钻石", "#日常首饰", "#珠宝分享", "#首饰分享", "#日常穿搭"]
情绪词 = ["#提升精致度的首饰", "#珠宝搭配", "#珠宝就要blingbling", "#行走的小灯泡",
          "#首饰就要闪闪发光的", "#把星星戴在身上", "#今天出门戴什么", "#长期主义",
          "#氛围感搭配小众首饰", "#一眼就爱上"]
情绪词_按品类 = {
    "项链": ["#宝藏项链分享", "#迷人的锁骨链", "#项链就要与众不同", "#高级感项链", "#百搭项链"],
    "手链": ["#手链分享", "#叠戴", "#手腕上的风景"],
    "耳饰": ["#耳饰分享", "#养耳洞", "#耳饰搭配"],
    "戒指": ["#戒指分享", "#叠戴出奇迹", "#晒晒我的钻戒"],
}


def build_tags(d, rng=None, n=9):
    """为一款生成 n 个小红书话题标签。
    固定:品牌词×2(#利奥星钻 + #培育钻石利奥星钻) + 该款黑话×1;其余从分品类池补齐、去重。"""
    r = rng or random
    _c = d.get("cat", "项链")
    cat = _CAT_NORM.get(_c, _c)
    tags = ["#利奥星钻", "#培育钻石利奥星钻"]
    黑 = d.get("黑话") or []
    if 黑:
        tags.append(r.choice(黑))
    # 候选池:品类词 + 分品类情绪词 + 通用情绪 + 通用词,打乱后补齐
    pool = (品类词.get(cat, []) + 情绪词_按品类.get(cat, []) + 情绪词 + 通用词)
    r.shuffle(pool)
    for x in pool:
        if len(tags) >= n:
            break
        if x not in tags:
            tags.append(x)
    return tags


def tags_str(d, rng=None, n=9):
    return " ".join(build_tags(d, rng, n))


# ============================ 标题+正文(GPT 提示词) ============================
_SYS = (
    "你是利奥星钻的小红书素人种草号写手,给指定的一款珠宝写【一篇】真实的种草笔记(标题 + 正文)。"
    "你写的不是广告,是一个普通用户分享自己买到的东西——要让人相信是真人写的,不是商家、不是 AI。"
    "核心目标:精准命中买家决策点,同时提前对齐预期(夸大到无法兑现只会换来差评)。"
)

_TONE_RULES = {
    "碎碎念": (
        "【口吻:碎碎念】像一个真实用户絮絮叨叨分享:可带一个具体人设(通勤党/宝妈/学生党/刚工作两年…)、"
        "一句纠结过程(比如在购物车躺了小半年、问了客服十几轮)、戴了多久的真实感受(戴了一个月/一个冬天)、"
        "有具体生活细节;语气自然口语、可有小语气词,结尾可带一句问句引导评论(姐妹们你们戴哪种?)。"
        "emoji 可用 1-3 个,不要堆。"
    ),
    "清冷": (
        "【口吻:清冷质感】平静、克制地描述质感,不喊不叫、不用'绝了/爱疯/谁懂啊'这种情绪词;"
        "全篇只用最多 1 个 emoji;说'耐看/越戴越顺眼/日常戴刚好',不说'惊艳';"
        "开头可以'先承认平淡、再说好'(比如:没有很夸张的设计,就是…… 但是上身很秀气……);"
        "整体像一个审美克制的人随手记录。"
    ),
}

_STRUCT = (
    "【必须包含】① 一句能当钩子的标题(不超过 20 字,可带 0-1 个 emoji);"
    "② 正文里自然写到:这款的规格/大小(用给你的'说法'把大小讲清楚)、材质、工艺特点、至少 1 个卖点;"
    "③ 必须【主动说一个短板/要注意的点】(用给你的'短板'),承认缺点反而真实、降退货;"
    "④ 正文尽量写到 180 字以上(小红书正文越具体越吃流量),但别注水。"
    "【绝对不能写】保值/升值/回收、和天然钻一模一样、全网最低/最闪/第一、编造证书机构名。"
    "【输出格式】第一行只写标题;然后空一行;后面是正文。不要写'标题:''正文:'这类字样,不要输出话题标签(标签另生成)。"
)


def build_copy_messages(d, spec=None, tone="混合", rng=None):
    """构造给 chat.completions 的 messages。
    d=该款产品 dict;spec=选用的规格(可选);tone: '碎碎念'/'清冷'/'混合'。"""
    r = rng or random
    real_tone = tone
    if tone == "混合":
        # 学 writer5:碎碎念 7 : 清冷 3
        real_tone = "碎碎念" if r.random() < 0.7 else "清冷"

    规格 = d.get("规格") or []
    if not spec:
        spec = r.choice(规格) if 规格 else ""
    说法 = (d.get("说法") or {}).get(spec, "") if spec else ""
    直径 = (d.get("直径") or {}).get(spec, "") if spec else ""
    卖点 = d.get("卖点") or []
    短板 = d.get("短板") or []

    info = (
        f"【款式】{d.get('昵称','')}({d.get('cat','')})\n"
        f"【规格/大小】{spec}{('。' + 说法) if 说法 else ''}{('。直径 ' + 直径) if 直径 else ''}\n"
        f"【材质】{d.get('材质','')}\n"
        f"【工艺】{d.get('工艺','')}——{d.get('工艺描述','')}\n"
        f"【卖点(自然融入,别硬广)】{'；'.join(卖点)}\n"
        f"【短板(必须主动提到其中一点)】{'；'.join(短板)}"
    )
    user = (
        f"{_TONE_RULES.get(real_tone, _TONE_RULES['碎碎念'])}\n\n"
        f"{_STRUCT}\n\n"
        f"这一篇写这款,信息如下(严格按这些事实,不要编造额外参数):\n{info}\n\n"
        f"现在写这一篇(标题 + 空行 + 正文)。"
    )
    messages = [{"role": "system", "content": _SYS},
                {"role": "user", "content": user}]
    return messages, real_tone, spec


def split_title_body(text):
    """把 GPT 输出拆成 (标题, 正文)。第一行非空当标题,其余当正文。"""
    lines = [l.rstrip() for l in (text or "").strip().splitlines()]
    lines = [l for l in lines]
    title, body = "", ""
    # 找第一行非空作标题
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        title = lines[idx].strip().lstrip("#").strip()
        body = "\n".join(lines[idx + 1:]).strip()
    if not body:  # 没有正文就把全部当正文
        body = (text or "").strip()
    return title, body


# ============================ 产品库表格(方案B:同事在 Excel 里加行改款)============================
# 一张独立 Excel,同事维护;App 里上传即用。列顺序如下(表头带说明也能认,按列名开头匹配)。
TEMPLATE_COLS = [
    "款式名称", "类型(项链/手链/耳饰/戒指/吊坠/手镯)", "规格(多个用;分隔)", "材质",
    "工艺", "工艺描述(镶嵌特征,越具体越准)", "卖点(多条用;分隔)", "短板(多条用;分隔)",
    "黑话标签(多个用;带#)", "说法(可选:规格=说法;规格=说法)",
]


def _split(s):
    # 多值只按分号/换行拆;不拆顿号「、」(它常出现在单条卖点内部,如"进光多、火彩活")
    return [x.strip() for x in re.split(r"[;；\n]+", str(s or "")) if x and str(x).strip()]


def _parse_shuofa(s):
    out = {}
    for seg in _split(s):
        if "=" in seg:
            k, v = seg.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def load_products_from_xlsx(file_like):
    """从上传的产品库 Excel 解析出 PRODUCTS 结构。按列名开头匹配,容忍表头带说明文字。
    跳过空行和以'例/('开头的说明行。返回 {款式名: {...}}。"""
    import openpyxl
    wb = openpyxl.load_workbook(file_like, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}
    header = [str(c).strip() if c is not None else "" for c in rows[0]]

    def col(*keys):
        for i, h in enumerate(header):
            for k in keys:
                if h.startswith(k):
                    return i
        return None

    ci = {
        "名称": col("款式名称", "名称"), "类型": col("类型"), "规格": col("规格"),
        "材质": col("材质"), "工艺描述": col("工艺描述"), "工艺": col("工艺"),
        "卖点": col("卖点"), "短板": col("短板"), "黑话": col("黑话"), "说法": col("说法"),
    }

    def cell(r, key):
        i = ci.get(key)
        return r[i] if (i is not None and i < len(r)) else None

    prods = {}
    for r in rows[1:]:
        if not r:
            continue
        name = cell(r, "名称")
        if not name:
            continue
        name = str(name).strip()
        if not name or name[0] in "(（例#":
            continue
        cat = (str(cell(r, "类型") or "项链").strip() or "项链")
        黑 = _split(cell(r, "黑话"))
        黑 = [x if x.startswith("#") else "#" + x for x in 黑]
        prods[name] = {
            "cat": cat, "昵称": name, "黑话": (黑 or ["#" + name]),
            "规格": _split(cell(r, "规格")), "直径": {},
            "材质": str(cell(r, "材质") or "").strip(),
            "工艺": str(cell(r, "工艺") or "").strip(),
            "工艺描述": str(cell(r, "工艺描述") or "").strip(),
            "卖点": _split(cell(r, "卖点")), "短板": _split(cell(r, "短板")),
            "说法": _parse_shuofa(cell(r, "说法")),
        }
    return prods
