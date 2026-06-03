#!/usr/bin/env python3
"""
买家秀批量生成脚本
==================
输入:
  1) 白底首饰图 (jewelry.png)  —— 决定首饰的样式 / 细节 / 材质
  2) 模特佩戴图 (wearing.jpg)  —— 决定佩戴部位 / 比例 / 大小

输出:
  多张不同生活场景的「买家秀」图片,iPhone 实拍质感,主打真实。

用法:
  pip install openai
  export OPENAI_API_KEY="sk-..."
  python buyer_show.py --jewelry ./jewelry.png --wearing ./wearing.jpg --out ./output

说明:
  使用 OpenAI images.edit 接口,模型 gpt-image-2,一次同时传入两张参考图。
  第 1 张(白底图)作为首饰真值,第 2 张(佩戴图)作为佩戴比例真值。
"""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

MODEL = "gpt-image-2"
SIZE = "1024x1536"        # 竖图,更接近手机随手拍 / 社交媒体买家秀比例
QUALITY = "high"          # gpt-image-2: low / medium / high

# ---------------------------------------------------------------------------
# 通用约束:每段场景 prompt 都会拼上这段「铁律」,锁死首饰细节与佩戴比例。
# ---------------------------------------------------------------------------
FIDELITY_RULES = """
【最高优先级 · 不可违背】
- 第一张参考图是这件首饰的白底标准图,请把它当作唯一真值:款式、轮廓、链条/戒圈结构、
  宝石数量与切面、镶嵌方式、金属色(金/银/玫瑰金)、表面纹理、刻字与logo,
  必须与第一张图 100% 一致。严禁增加、删除、简化或改变任何细节,严禁更换款式。
- 第二张参考图是真人佩戴图,请严格参照其中首饰相对于人体(耳/颈/手/腕)的
  佩戴位置、朝向、相对比例与大小。成品中首饰的大小比例必须与第二张图一致,
  不可放大成夸张尺寸,也不可缩小到看不清。
- 只改变「场景、人物、光线、氛围」,绝不改变「首饰本身」。
- 首饰必须清晰、对焦准确、是画面视觉重点之一,但不能假到像产品广告。

【真实买家秀质感 · iPhone 直出原图,这点非常重要】
- 必须像 iPhone 随手拍、没有修过的原图:真实、普通、生活化,而不是精修网红照。
- 明确允许并鼓励这些"不完美":轻微失焦/模糊、手抖糊一点、对焦没对准、
  光线不理想(偏暗、逆光、过曝、白平衡偏色都可以)、构图随意甚至略歪、
  画面有真实噪点和颗粒感、阴影杂乱、背景普通凌乱。
- 严禁:影棚打光、过度磨皮、磨成无毛孔的网红脸、完美对称构图、广告大片感、
  HDR 过度、CG/渲染感、水印文字、美颜滤镜痕迹。
- 皮肤要有真实质感:毛孔、细纹、轻微瑕疵都保留,不要假滑。
- 整体就像普通女生随手拍了发朋友圈/小红书的生活照,接地气、有烟火气。

【不露脸 · 必须遵守】
- 不要拍到完整的脸。最多只露半张脸(侧脸、下半张脸、或脸的一小部分)。
- 多用这些方式避免露脸:只拍颈部到锁骨、只拍手部、背对/侧对镜头露后颈、
  低头让头发遮住脸、脸转向画面外、或脸在画框之外。
- 重点始终是首饰本身,不是人物的脸。

【着装与画面健康度 · 必须遵守】
- 人物穿着得体、保守、日常,衣着完整覆盖身体,绝不暴露、不低胸、不强调胸部或身材。
- 镜头重点放在首饰以及颈部、锁骨、耳部、手部,避免把画面聚焦在胸口区域。
- 画面整体健康大方,适合公开电商平台展示,不含任何性暗示。

【只允许出现这一件首饰 · 必须遵守】
- 画面中只能出现第一张参考图里的这一件首饰。
- 严禁额外添加任何参考图里没有的首饰(不要凭空加戒指、手链、耳环、其它项链等)。

【保持类型与造型不变 · 必须遵守】
- 首饰类型不能变:手链就是手链、项链就是项链、耳钉就是耳钉、戒指就是戒指,
  严禁把一种类型画成另一种(例如把手链画成项链戴到脖子上)。
- 链子的样式、扣头、吊坠/charm 的造型轮廓、钻石的数量与排列方式,
  必须和第一张参考图完全一致,不得简化、不得改设计、不得换形状。
"""

# ---- 首饰类型 -> 强制佩戴部位的指令 ----
# key 与网页下拉框一致;每条会被放在该批每张图提示词的最前面,优先级最高。
JEWELRY_TYPES = {
    "手链": "这是一条【手链】。佩戴时必须、且只能戴在手腕上,镜头聚焦手腕,绝不能戴到脖子、耳朵或手指上。",
    "项链": "这是一条【项链/吊坠】。佩戴时必须、且只能戴在脖子上(颈部到锁骨),镜头聚焦颈部,绝不能画成手链、耳饰或戒指。",
    "耳钉/耳环": "这是一对【耳饰】。佩戴时必须、且只能戴在耳垂上,镜头聚焦耳朵和耳侧,绝不能画成项链、手链或戒指。",
    "戒指": "这是一枚【戒指】。佩戴时必须、且只能戴在手指上,镜头聚焦手指和手部,绝不能画成项链、手链或耳饰。",
    "手镯": "这是一只【手镯】。佩戴时必须、且只能戴在手腕上,镜头聚焦手腕,绝不能戴到脖子、耳朵或手指上。",
    "脚链": "这是一条【脚链】。佩戴时必须、且只能戴在脚踝上,镜头聚焦脚踝。",
    "自动判断": "请根据参考图和佩戴图自行判断这件首饰的类型(项链/手链/耳饰/戒指等),并严格戴在该类型对应的正确部位,不得改变类型。",
}

# ---------------------------------------------------------------------------
# 固定一套场景,每个款式都用同一套,顺序与内容写死,保证每款一致。
# 默认配比:真人佩戴 10 张 + 手拿 4 张 + 首饰盒/静物 4 张 = 18 张。
# 全部不露脸(最多半脸);worn 的动作【不指定部位】,部位由首饰类型指令决定。
# ---------------------------------------------------------------------------
N_WORN, N_HELD, N_BOX = 10, 4, 4  # 一个款式固定出 18 张

WORN_TPL = "类型:真人佩戴(不露脸,最多半脸)。{body} 像随手拍发朋友圈/小红书的生活照,自然、不完美、有烟火气。"
HELD_TPL = "类型:手拿首饰展示(画面里没有脸)。{body} 像随手拍的,允许轻微模糊和不理想的光。"
BOX_TPL = "类型:首饰盒/静物摆拍(画面里没有人、没有脸)。{body} 像收到货随手拍的实物图,真实、生活化,不要广告感。"

# ---- 真人佩戴(10 张正式 + 备用) ----
WORN_FIXED = [
    "场景:午后靠窗的咖啡馆,木桌上有杯拿铁。侧身低头,长发垂下遮住大半张脸,镜头特写佩戴部位。光线是午后斜射的自然光,略微过曝。",
    "场景:卧室镜子前自拍,背景有床和随手放的衣物。用手机半挡住脸,只露佩戴部位。光线是室内暖黄灯,整体偏暗有噪点。",
    "场景:傍晚的城市街头,背景虚化的招牌和行人。侧身只露半张脸看向画面外,镜头落在佩戴部位。光线是黄昏暖金色余晖,不太均匀。",
    "场景:早晨的梳妆台前,台面有化妆品和香水瓶。低头整理仪容,头发遮住脸,佩戴部位近景。光线是靠窗大片自然光,略发白。",
    "场景:周末家里的沙发上,旁边是抱枕和毛毯。慵懒侧靠,手自然搭着,脸只露一点点,镜头对着佩戴部位。光线是室内暖光偏暗。",
    "场景:阳台小桌边,有杯茶和一本书。侧脸转向窗外只露半脸,佩戴部位被光照亮。光线是清晨侧逆光,有点偏暗。",
    "场景:公园长椅上,背景是绿树和斑驳树影。侧坐,脸在画框外,自然展示佩戴部位。光线是阴天均匀柔和的散射光。",
    "场景:地铁车厢里,背景虚化的车窗。侧身抓拍的随手一张,脸基本不入镜。光线是车厢白光,白平衡偏冷。",
    "场景:家里飘窗读书角,落地窗洒进大片自然光。低头看书,头发遮脸,佩戴部位清晰。光线大片自然光,背景过曝发白。",
    "场景:下午茶甜品店,桌上有蛋糕和咖啡。手自然搭在桌上,佩戴部位特写,脸最多露半张。光线是暖黄灯光,温馨偏暗。",
    "场景:花店门口,背景大量虚化的鲜花绿植。半侧背对镜头,露出佩戴部位。光线是户外柔和散射光。",
    "场景:汽车副驾上,系着安全带,车窗外自然光。侧脸只露半张,佩戴部位入镜。光线是车窗自然光,略逆光。",
]

# ---- 手拿展示(4 张) ----
HELD_FIXED = [
    "一只手的指尖捏着这件首饰,举到窗边对着自然光看,背景是虚化的居家环境,只有手和首饰入镜。光线是窗边自然光,略微失焦。",
    "摊开手心托着这件首饰,手机俯拍,背景是浅色衣服或桌面,没有人脸。光线偏暗,有真实噪点。",
    "刚拆快递,手捏着首饰举起来,背景是拆开的包装盒和桌面,生活感强。光线是室内灯光,白平衡偏黄。",
    "手指捏着这件首饰凑近端详,只见手和首饰,背景虚化。光线一般,像随手一拍,略糊。",
]

# ---- 首饰盒/静物(4 张) ----
BOX_FIXED = [
    "这件首饰放在打开的首饰盒绒布上,旁边有梳妆台的小物,窗边自然光,手机俯拍静物。构图随意,略偏暗。",
    "首饰摆在米色绒布或亚麻布上,旁边随手放着一支口红或几朵干花,俯拍。光线是柔和自然光。",
    "首饰放在浅色大理石或木质桌面上,旁边有杯咖啡,自然光,构图随意。光线略过曝,像随手一拍。",
    "首饰躺在首饰盒里,放在床头柜上,暖黄灯光,随手拍的,略暗有噪点。",
]


def _scene(idx, kind, directive, body):
    # directive(首饰类型指令)放最前面,优先级最高
    prompt = f"\n【首饰类型 · 最高优先级】{directive}\n{body}\n"
    return {"name": f"scene_{idx:02d}_{kind}", "prompt": prompt}


def build_scene_pool(n=None, rng=None, jewelry_type: str = "自动判断"):
    """返回固定的 18 张场景(佩戴10 + 手拿4 + 盒4),顺序写死。
    jewelry_type 决定佩戴部位。n / rng 参数保留以兼容旧调用,不再使用。"""
    directive = JEWELRY_TYPES.get(jewelry_type, JEWELRY_TYPES["自动判断"])

    plan = (
        [("worn", WORN_TPL.format(body=b)) for b in WORN_FIXED[:N_WORN]]
        + [("held", HELD_TPL.format(body=b)) for b in HELD_FIXED[:N_HELD]]
        + [("box", BOX_TPL.format(body=b)) for b in BOX_FIXED[:N_BOX]]
    )

    scenes = []
    for i, (kind, body) in enumerate(plan, 1):
        scenes.append(_scene(i, kind, directive, body))
    return scenes


def to_image_file(path: str):
    """打开图片文件,返回可传给 API 的文件对象。"""
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到文件: {path}")
    return open(p, "rb")


def generate(client: OpenAI, jewelry_path: str, wearing_path: str,
             scene: dict, out_dir: Path, retries: int = 2):
    """对单个场景调用 API 并保存结果。"""
    full_prompt = FIDELITY_RULES + "\n【本张场景】" + scene["prompt"]

    for attempt in range(1, retries + 2):
        try:
            # 每次重新打开文件句柄(请求会消费它)
            with to_image_file(jewelry_path) as f1, to_image_file(wearing_path) as f2:
                result = client.images.edit(
                    model=MODEL,
                    image=[f1, f2],          # 多图参考:白底图 + 佩戴图
                    prompt=full_prompt,
                    size=SIZE,
                    quality=QUALITY,
                    n=1,
                )
            b64 = result.data[0].b64_json
            out_path = out_dir / f"{scene['name']}.png"
            out_path.write_bytes(base64.b64decode(b64))
            print(f"  ✓ 已生成 {out_path.name}")
            return out_path
        except Exception as e:
            print(f"  ! {scene['name']} 第 {attempt} 次失败: {e}")
            if attempt <= retries:
                time.sleep(3 * attempt)
            else:
                print(f"  ✗ {scene['name']} 放弃")
                return None


def main():
    import random

    ap = argparse.ArgumentParser(description="批量生成首饰买家秀")
    ap.add_argument("--jewelry", required=True, help="白底首饰图路径")
    ap.add_argument("--wearing", required=True, help="模特佩戴图路径")
    ap.add_argument("--out", default="./output", help="输出目录")
    ap.add_argument("--count", type=int, default=6, help="生成几张(默认 6)")
    ap.add_argument("--seed", type=int, help="随机种子,固定后可复现同一批场景")
    ap.add_argument("--type", default="自动判断",
                    help="首饰类型: 手链/项链/耳钉耳环/戒指/手镯/脚链/自动判断")
    args = ap.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("请先设置环境变量 OPENAI_API_KEY")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    scenes = build_scene_pool(args.count, rng, args.type)

    client = OpenAI()
    print(f"随机抽取 {len(scenes)} 个场景,模型 {MODEL},输出到 {out_dir.resolve()}\n")
    ok = 0
    for scene in scenes:
        print(f"[{scene['name']}] 生成中...")
        if generate(client, args.jewelry, args.wearing, scene, out_dir):
            ok += 1

    print(f"\n完成: {ok}/{len(scenes)} 张成功。")


if __name__ == "__main__":
    main()
