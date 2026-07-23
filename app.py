#!/usr/bin/env python3
"""
珠宝图片生成器 · 网页版 (Streamlit)
=====================================
两个标签:
  1) 买家秀 —— iPhone 生活感、不露脸的真实晒单图
  2) 电商精修图 —— 5 家店铺风格、高级珠宝棚拍图(3 模特图 + 3 场景图)
API key 集中放在服务器(st.secrets / 环境变量),用户不接触。
"""

import base64
import datetime
import io
import os
import zipfile

import streamlit as st
from openai import OpenAI

# 版本号:三个文件必须一致;页面底部自动校验,不一致会红字报警(=有文件没传齐)
VERSION = "3.5"

# 每次生成自动保存到脚本同目录下的 outputs/ 文件夹,按时间分批
OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

import buyer_show as _bs_mod
import ecommerce as _ec_mod
from buyer_show import (
    FIDELITY_RULES,
    BUYER_FACE_OVERRIDE,
    MODEL,
    SIZE,
    QUALITY,
    JEWELRY_TYPES,
    build_scene_pool,
    build_grouped_scenes,
)
from ecommerce import (
    SHOPS,
    SHOP_KEYS,
    ECOM_SIZE,
    ECOM_QUALITY,
    build_ecommerce_jobs,
    digital_model_prompts,
)

st.set_page_config(page_title="珠宝图片生成器", page_icon="💎", layout="wide")

# 文案生成用的文字模型(便宜够用;如失效可改成账号里可用的其它文本模型)
COPY_MODEL = "gpt-4o-mini"


# ===========================================================================
# 通用工具
# ===========================================================================
def get_api_key():
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:
        pass
    return os.getenv("OPENAI_API_KEY")


def get_app_password():
    try:
        if "APP_PASSWORD" in st.secrets:
            return st.secrets["APP_PASSWORD"]
    except Exception:
        pass
    return os.getenv("APP_PASSWORD")


def require_password():
    pw = get_app_password()
    if not pw:
        return
    if st.session_state.get("authed"):
        return
    st.markdown("### 🔒 请输入访问口令")
    entered = st.text_input("口令", type="password")
    if st.button("进入"):
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("口令不对,请重试。")
    st.stop()


def to_named_bytes(uploaded, fallback_name):
    data = uploaded.getvalue()
    name = getattr(uploaded, "name", fallback_name) or fallback_name
    return (name, data)


def _read_png(v):
    """结果在会话里存的是磁盘路径(省内存防崩溃);按需读出 bytes。兼容旧的 bytes。"""
    if isinstance(v, (bytes, bytearray)):
        return v
    with open(v, "rb") as f:
        return f.read()


def new_run_dir():
    d = os.path.join(OUTPUT_ROOT, datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def check_versions():
    """三个文件版本必须一致;不一致 = 有文件没传齐,顶部红字报警。"""
    versions = {
        "app.py": VERSION,
        "ecommerce.py": getattr(_ec_mod, "VERSION", "旧版未更新"),
        "buyer_show.py": getattr(_bs_mod, "VERSION", "旧版未更新"),
    }
    detail = " | ".join(f"{k} = v{v}" for k, v in versions.items())
    if len(set(versions.values())) != 1:
        st.error(f"⚠️ 文件版本不一致,有文件没传齐!{detail}\n\n"
                 "请把 app.py、ecommerce.py、buyer_show.py 三个文件一起传到 GitHub 覆盖,再 reboot。")
    else:
        st.caption(f"✅ 版本 v{VERSION} · 三个文件版本一致")


def zip_download(results, fname):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, png in results:
            zf.writestr(f"{name}.png", _read_png(png))
    st.download_button("📦 打包下载全部", data=buf.getvalue(),
                       file_name=fname, mime="application/zip",
                       use_container_width=True)


# ===========================================================================
# 买家秀文案生成(文字模型,不生图、几分钱一次)
# ===========================================================================
def generate_review_copy(client, title, sample, n=3):
    """根据宝贝标题 / 文案范例,生成 n 条真实、无 AI 味的买家秀文案(每条 80-160 字)。"""
    sys_prompt = (
        "你是一个真实的珠宝网店买家,刚收到并戴上了首饰,在淘宝/小红书写真实评价。"
        "写作要求:口语化、有具体生活细节、像真人随手写的;绝对不能有 AI 腔和模板感,"
        "不要华丽排比、不要堆砌形容词、不要每条开头都一样。可以有一点点小口误感、小语气词,自然真实。"
    )
    user_prompt = (
        f"【宝贝标题】{title or '(未提供)'}\n"
        f"【文案范例 · 模仿它的语气和角度,但内容要不同,不要抄】\n{sample or '(未提供)'}\n\n"
        f"请写 {n} 条【不同】的买家秀好评,每条 80-160 个字。"
        "内容要像真的买过、戴过、用过的人写的真实感受;每条只挑 1-2 个点自然地夸,不要把所有点堆在一条里。"
        "可写的角度(自选):客服态度好/耐心、发货快、包装精美有仪式感、钻石很闪很亮、做工精致甚至比专柜还好、"
        "戴上显手白/显气质、性价比高、复购/推荐朋友 等。"
        "每条可带 0-1 个 emoji(也可不带)。"
        "只输出文案本身,每条之间空一行,不要写编号、不要写任何解释或标题。"
    )
    r = client.chat.completions.create(
        model=COPY_MODEL,
        messages=[{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": user_prompt}],
        temperature=0.95,
    )
    return r.choices[0].message.content.strip()


# ===========================================================================
# 买家秀:盒子参考图 + 生成
# ===========================================================================
BOX_FILES = {"box_black": "box_black", "box_burgundy": "box_burgundy"}
_IMG_EXTS = [".png", ".jpg", ".jpeg", ".webp"]


def load_box_images():
    here = os.path.dirname(os.path.abspath(__file__))
    boxes = {}
    for ref, stem in BOX_FILES.items():
        for ext in _IMG_EXTS:
            path = os.path.join(here, stem + ext)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    boxes[ref] = (stem + ext, f.read())
                break
    return boxes


def generate_one(client, jewelry, second, scene, quality=QUALITY, show_face=False):
    """买家秀单张:拼买家秀铁律(iPhone 真实感/不露脸)。show_face=True 时允许露脸。"""
    full_prompt = FIDELITY_RULES + "\n【本张场景】" + scene["prompt"]
    if show_face:
        full_prompt += BUYER_FACE_OVERRIDE
    images = [(jewelry[0], io.BytesIO(jewelry[1]))]
    if second is not None:
        images.append((second[0], io.BytesIO(second[1])))
    result = client.images.edit(model=MODEL, image=images, prompt=full_prompt,
                                size=SIZE, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def pick_second_ref(scene, wearing, boxes):
    ref = scene.get("ref", "wearing")
    if ref == "wearing":
        return wearing
    if ref in boxes:
        return boxes[ref]
    if ref in BOX_FILES:
        return None
    return None


def assign_qualities(scenes, n_high, low_tier):
    priority = {"box": 0, "held": 1, "worn": 2}
    order = sorted(range(len(scenes)),
                   key=lambda i: priority.get(scenes[i]["name"].split("_")[-1], 3))
    high_idx = set(order[:n_high])
    return ["high" if i in high_idx else low_tier for i in range(len(scenes))]


# ===========================================================================
# 电商精修图:生成(高级棚拍,不拼买家秀铁律)
# ===========================================================================
def load_shop_models(shop):
    """按店铺加载专属数字模特三张参考图,返回 {role:(name,bytes)}。
    model_<key>=脖子 / model_<key>2=手 / model_<key>3=耳朵。"""
    key = SHOP_KEYS.get(shop)
    out = {}
    if not key:
        return out
    here = os.path.dirname(os.path.abspath(__file__))
    roles = {"neck": f"model_{key}", "hand": f"model_{key}2", "ear": f"model_{key}3"}
    for role, stem in roles.items():
        for ext in _IMG_EXTS:
            path = os.path.join(here, stem + ext)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    out[role] = (stem + ext, f.read())
                break
    return out


def select_model_refs(models, jtype):
    """按首饰类型挑该用的模特参考:项链→脖子、耳饰→耳朵+脖子、戒指/手链→手、自动→全部。"""
    if not models:
        return []
    def pick(keys):
        got = [models[k] for k in keys if k in models]
        return got or list(models.values())
    if jtype == "项链":
        return pick(["neck"])
    if jtype in ("耳饰", "耳钉/耳环"):
        return pick(["ear", "neck"])
    if jtype in ("戒指", "手链", "手镯"):
        return pick(["hand"])
    return list(models.values())  # 自动判断:全给


def generate_ecom(client, ref_images, prompt, quality=ECOM_QUALITY):
    """ref_images 是参考图列表 [(name,bytes), ...](最多 16 张):产品整套图 +(模特图)。"""
    imgs = [(n, io.BytesIO(b)) for (n, b) in ref_images[:16]]
    result = client.images.edit(model=MODEL, image=imgs, prompt=prompt,
                                size=ECOM_SIZE, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def upscale_png(png_bytes, scale):
    """把 PNG 放大 scale 倍(Lanczos),用于提高输出分辨率。scale<=1 原样返回。"""
    if scale <= 1:
        return png_bytes
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def generate_digital_model(client, shop):
    """为店铺生成数字模特三张参考图:① 肩颈(文生图) ② 手部 ③ 侧脸耳朵(均参考①保持同一人)。"""
    neck_p, hand_p, ear_p = digital_model_prompts(shop)
    r1 = client.images.generate(model=MODEL, prompt=neck_p, size=ECOM_SIZE, quality=ECOM_QUALITY, n=1)
    neck_png = base64.b64decode(r1.data[0].b64_json)

    def edit_from_neck(prompt):
        r = client.images.edit(model=MODEL, image=[("neck.png", io.BytesIO(neck_png))],
                               prompt=prompt, size=ECOM_SIZE, quality=ECOM_QUALITY, n=1)
        return base64.b64decode(r.data[0].b64_json)

    hand_png = edit_from_neck(hand_p)
    ear_png = edit_from_neck(ear_p)
    return neck_png, hand_png, ear_png


# ===========================================================================
# 标签 1:买家秀
# ===========================================================================
def render_buyer_show(api_key):
    st.caption("上传白底首饰图 + 模特佩戴图,自动生成不同生活场景的真实买家秀(iPhone 直出感、不露脸)。")

    col1, col2 = st.columns(2)
    with col1:
        jewelry_file = st.file_uploader("① 白底首饰图(决定款式 / 细节)",
                                        type=["png", "jpg", "jpeg", "webp"], key="bs_jewelry")
        if jewelry_file:
            st.image(jewelry_file, caption="首饰真值图", use_container_width=True)
    with col2:
        wearing_file = st.file_uploader("② 模特佩戴图(决定佩戴比例 / 大小)",
                                        type=["png", "jpg", "jpeg", "webp"], key="bs_wearing")
        if wearing_file:
            st.image(wearing_file, caption="佩戴比例参照图", use_container_width=True)

    jewelry_type = st.selectbox("首饰类型(决定戴在哪个部位,务必选对)",
                                options=["手链", "项链", "耳钉/耳环", "戒指", "手镯", "脚链", "自动判断"],
                                index=0, key="bs_type")
    scol1, scol2 = st.columns(2)
    with scol1:
        season = st.selectbox("季节(决定穿搭)", options=["不限", "夏天", "冬天"], index=0, key="bs_season")
    with scol2:
        env = st.selectbox("场景环境", options=["不限", "室内", "户外", "轻奢日常"], index=0, key="bs_env",
                           help="轻奢日常:iPhone 俯拍手部特写、多件叠戴 + 奢牌手袋/皮鞋压角、暖调家居氛围(微购相册富家太太风)")

    mode = st.radio("生成模式", options=["分场景(每场景3张·同一买家)", "无要求(18张各不相同)"],
                    index=0, horizontal=True, key="bs_mode")
    grouped = mode.startswith("分场景")
    if grouped:
        n_scenes = st.slider("场景数量(每个场景出 3 张)", 1, 6, 6, key="bs_nscenes")
        st.caption(f"{n_scenes} 个场景 × 每场景 3 张 = {n_scenes*3} 张;每组 3 张是同一买家/同一场景的不同角度。")
    else:
        n_scenes = 6
        st.caption("18 张各不相同:真人佩戴 10 + 手拿 4 + 首饰盒/静物 4,全部不露脸。")

    qcol1, qcol2 = st.columns(2)
    with qcol1:
        n_high = st.slider("其中高画质(high)张数", 0, 18, 4, key="bs_high")
    with qcol2:
        low_tier = st.selectbox("其余张数的画质", options=["medium", "low"], index=0, key="bs_low")

    show_face = st.radio("模特脸部", options=["不露脸(推荐)", "露脸"],
                         index=0, horizontal=True, key="bs_face") == "露脸"

    # ---- 买家秀文案生成(可选):一组场景 = 一套文案,条数默认跟场景数量一致 ----
    with st.expander("📝 顺便生成买家秀文案(真实、无 AI 味,80-160 字 · 一组一套)"):
        copy_title = st.text_input("宝贝标题(可只填这个)", key="bs_copy_title",
                                   placeholder="如:利奥星钻 培育钻石18K金 项链")
        copy_sample = st.text_area("好评范例(可只填这个,把你们利奥星钻真实好评粘进来,越像越好)",
                                   key="bs_copy_sample", height=100,
                                   placeholder="粘贴 1-3 条真实好评,AI 照这个语气写;标题和范例填一个即可,都填更准")
        copy_n = st.slider("生成几套文案", 1, 6, value=int(n_scenes), key="bs_copy_n",
                           help="默认 = 场景数量(几组买家秀就配几套文案),也可手动改")
        if st.button("✨ 生成文案", key="bs_copy_run"):
            if not (copy_title or "").strip() and not (copy_sample or "").strip():
                st.warning("宝贝标题和好评范例至少填一个。")
            else:
                try:
                    with st.spinner("正在写文案..."):
                        txt = generate_review_copy(OpenAI(api_key=api_key), copy_title, copy_sample, copy_n)
                    st.session_state["bs_copy_out"] = txt
                except Exception as e:
                    st.error(f"文案生成失败:{e}")
        if st.session_state.get("bs_copy_out"):
            st.text_area("生成结果(可直接复制;不满意再点一次重出)",
                         value=st.session_state["bs_copy_out"], height=260, key="bs_copy_show")

    run = st.button("🚀 开始生成", type="primary", use_container_width=True, key="bs_run")

    if run:
        if not jewelry_file or not wearing_file:
            st.warning("请先上传两张图片。")
        else:
            if grouped:
                scenes = build_grouped_scenes(jewelry_type=jewelry_type, season=season,
                                              env=env, n_scenes=n_scenes)
            else:
                scenes = build_scene_pool(jewelry_type=jewelry_type, season=season, env=env)
            qualities = assign_qualities(scenes, min(n_high, len(scenes)), low_tier)
            client = OpenAI(api_key=api_key)

            jewelry = to_named_bytes(jewelry_file, "jewelry.png")
            wearing = to_named_bytes(wearing_file, "wearing.png")
            boxes = load_box_images()
            if not boxes:
                st.warning("未检测到盒子参考图(box_black / box_burgundy),首饰盒场景将按文字描述生成。")

            run_dir = new_run_dir()
            results, group_base, group_base_path, items = [], {}, {}, []
            progress = st.progress(0.0, text="准备中...")
            preview = st.empty()
            for i, scene in enumerate(scenes, 1):
                q = qualities[i - 1]
                progress.progress((i - 1) / len(scenes), text=f"生成第 {i}/{len(scenes)} 张({q})...")
                try:
                    if scene.get("ref") == "base":
                        base_png = group_base.get(scene.get("group"))
                        second = ("base.png", base_png) if base_png else wearing
                        # ctx 里存基准图的磁盘路径而不是大图本体,省内存
                        bp = group_base_path.get(scene.get("group"))
                        second_ctx = ("base.png", bp) if (base_png and bp) else wearing
                    else:
                        second = pick_second_ref(scene, wearing, boxes)
                        second_ctx = second
                    png = generate_one(client, jewelry, second, scene, quality=q, show_face=show_face)
                    fpath = os.path.join(run_dir, f"{scene['name']}.png")
                    with open(fpath, "wb") as fp:
                        fp.write(png)
                    if scene.get("var") == 0 and scene.get("group") is not None:
                        group_base[scene["group"]] = png
                        group_base_path[scene["group"]] = fpath
                    results.append((scene["name"], fpath))  # 存路径不存大图,防内存爆掉
                    items.append({"scene": scene, "second": second_ctx, "q": q, "show_face": show_face})
                    preview.image(png, caption=f"刚生成:{scene['name']} · {q}", width=260)
                except Exception as e:
                    st.error(f"{scene['name']} 生成失败:{e}")
            progress.progress(1.0, text="完成")
            preview.empty()
            st.session_state["bs_results"] = results  # 存起来,点下载不丢
            # 存每张图的生成参数,供"单张重出"抽卡用
            st.session_state["bs_ctx"] = {"jewelry": jewelry, "items": items, "run_dir": run_dir}

    def _regen_bs(idx):
        """只重出买家秀第 idx 张(单张计费),其余不动。"""
        ctx = st.session_state.get("bs_ctx")
        results = st.session_state.get("bs_results") or []
        if not ctx or idx >= len(results) or idx >= len(ctx["items"]):
            st.warning("这张图没有保存生成参数(旧批次),请重新生成一批后再抽卡。")
            return
        it = ctx["items"][idx]
        name = results[idx][0]
        try:
            sec = it["second"]
            if sec and isinstance(sec[1], str):  # 基准图存的是路径,读回 bytes
                sec = (sec[0], _read_png(sec[1]))
            with st.spinner(f"正在重出 {name}(同场景重新抽一张)..."):
                png = generate_one(OpenAI(api_key=api_key), ctx["jewelry"],
                                   sec, it["scene"], quality=it["q"],
                                   show_face=it.get("show_face", False))
            fpath = os.path.join(ctx["run_dir"], f"{name}.png")
            with open(fpath, "wb") as fp:
                fp.write(png)
            results[idx] = (name, fpath)
            st.session_state["bs_results"] = results
            st.rerun()
        except Exception as e:
            st.error(f"{name} 重出失败:{e}")

    # 持久展示(从会话状态,点任意下载/刷新后都还在);每张可单独"重出"抽卡
    _render_results(st.session_state.get("bs_results"), "buyer_shows.zip", "bsdl", regen=_regen_bs)


def _render_results(results, zip_name, key_prefix, regen=None):
    """展示结果网格。regen 传一个 fn(idx) 时,每张图旁多一个"重出这张"按钮,
    只重新生成该张(单张计费),替换原图,其余不动。"""
    if not results:
        return
    st.success(f"成功生成 {len(results)} 张。")
    cols = st.columns(3)
    for idx, (name, stored) in enumerate(results):
        try:
            png = _read_png(stored)
        except Exception:
            continue  # 服务器重启后临时文件丢失,跳过
        with cols[idx % 3]:
            st.image(png, caption=name, use_container_width=True)
            if regen is None:
                st.download_button("下载这张", data=png, file_name=f"{name}.png",
                                   mime="image/png", key=f"{key_prefix}_{idx}_{name}")
            else:
                c1, c2 = st.columns(2)
                with c1:
                    st.download_button("下载这张", data=png, file_name=f"{name}.png",
                                       mime="image/png", key=f"{key_prefix}_{idx}_{name}")
                with c2:
                    if st.button("🎲 重出这张", key=f"{key_prefix}_re_{idx}",
                                 help="只重新生成这一张(单张计费),不满意可反复抽卡"):
                        regen(idx)
    zip_download(results, zip_name)


# ===========================================================================
# 标签 2:电商精修图
# ===========================================================================
def render_ecommerce(api_key):
    st.caption("选店铺风格 + 首饰类型,上传白底产品图,每款生成 3 张模特图 + 3 张场景图(3:4 高级珠宝棚拍)。")

    scol1, scol2 = st.columns(2)
    with scol1:
        shop = st.selectbox("店铺风格", options=list(SHOPS.keys()), index=0, key="ec_shop")
    with scol2:
        jtype = st.selectbox("首饰类型",
                             options=["项链", "吊坠", "戒指", "手链", "手镯", "耳饰", "胸针", "自动判断"],
                             index=0, key="ec_type")

    # 该店铺的专属数字模特(自动从仓库加载脖子/手/耳三张;也允许临时上传覆盖)
    shop_models = load_shop_models(shop)
    k = SHOP_KEYS[shop]
    if shop_models:
        st.success(f"已自动加载【{shop}】的专属数字模特(脖子/手/耳 共 {len(shop_models)} 张参考)。")
    else:
        st.info(f"未检测到【{shop}】的专属数字模特(model_{k} / model_{k}2 / model_{k}3)。"
                "可用下面的按钮生成,或临时上传一张。")

    # 一键生成该店数字模特(脖子图 + 手图 + 耳朵图),下载后命名传到仓库即固定
    with st.expander("🧑‍🎨 为本店生成数字模特(脖子图 + 手图 + 耳朵图)"):
        st.caption(f"按【{shop}】的肤色/气质生成同一个虚拟模特(无首饰、不露脸)。"
                   f"下载后分别改名为 model_{k} / model_{k}2 / model_{k}3,传到仓库即长期固定使用。")
        if st.button("生成数字模特(3 张)", key="ec_gen_model"):
            try:
                with st.spinner("正在生成数字模特(约 1 分钟)..."):
                    neck_png, hand_png, ear_png = generate_digital_model(OpenAI(api_key=api_key), shop)
                # 存进会话状态,避免点下载触发重跑后图片丢失
                st.session_state["dm_result"] = {"shop": shop,
                                                 "imgs": (neck_png, hand_png, ear_png)}
            except Exception as e:
                st.session_state.pop("dm_result", None)
                st.error(f"生成失败:{e}")

        # 从会话状态展示(刷新/下载后仍在,三张都能下载)
        res = st.session_state.get("dm_result")
        if res and res.get("shop") == shop:
            neck_png, hand_png, ear_png = res["imgs"]
            trio = [("肩颈图", f"model_{k}.png", neck_png),
                    ("手部图", f"model_{k}2.png", hand_png),
                    ("耳朵图", f"model_{k}3.png", ear_png)]
            mcols = st.columns(3)
            for col, (label, fname, png) in zip(mcols, trio):
                with col:
                    st.image(png, caption=f"{label} → 存为 {fname}", use_container_width=True)
                    st.download_button(f"下载{label}", data=png, file_name=fname,
                                       mime="image/png", key=f"ec_dl_{fname}")
            st.info("满意就下载这三张,改成上面的文件名传到 GitHub 仓库;不满意可再点一次重新生成。")

    prod_files = st.file_uploader(
        "① 该款产品的整套参考图(可多张:不同角度的白底图 / 场景图,越多越准越多样)",
        type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="ec_prod")
    if prod_files:
        st.image([f for f in prod_files][:8], width=110)
    model_files = st.file_uploader("② 临时模特参考图(可选,可多张,覆盖本店默认模特)",
                                   type=["png", "jpg", "jpeg", "webp"],
                                   accept_multiple_files=True, key="ec_model")
    if model_files:
        st.image([f for f in model_files][:6], width=110)

    gcol1, gcol2 = st.columns(2)
    with gcol1:
        gen_what = st.radio("生成内容(可只出一种,方便抽卡替换)",
                            options=["模特图 + 场景图", "只出模特图", "只出场景图"],
                            index=0, key="ec_what")
        include = {"模特图 + 场景图": "both", "只出模特图": "model", "只出场景图": "scene"}[gen_what]
    with gcol2:
        out_scale = st.selectbox("输出尺寸", options=["放大2倍(约2048×3072)", "放大到4K(约2730×4096)", "标准(1024×1536)"],
                                 index=0, key="ec_scale")
    scale = {"标准(1024×1536)": 1.0, "放大2倍(约2048×3072)": 2.0, "放大到4K(约2730×4096)": 2.67}[out_scale]

    ec_show_face = st.radio("模特脸部(仅影响模特图)", options=["不露脸(推荐)", "露脸"],
                            index=0, horizontal=True, key="ec_face") == "露脸"

    st.caption("模特图=局部特写(下巴/锁骨/手等,不露脸);场景图按首饰类型自动定呈现(链状平铺成弧、戒指立起/平放等)。整套参考图喂得越全越准。")

    run = st.button("🚀 生成电商图", type="primary", use_container_width=True, key="ec_run")

    if run:
        if not prod_files:
            st.warning("请先上传至少一张产品参考图。")
        else:
            client = OpenAI(api_key=api_key)
            product_refs = [to_named_bytes(f, f"product_{i}.png") for i, f in enumerate(prod_files)]
            if model_files:
                model_refs = [to_named_bytes(f, f"model_{i}.png") for i, f in enumerate(model_files)]
            else:
                model_refs = select_model_refs(shop_models, jtype)
            jobs = build_ecommerce_jobs(shop, jtype, has_model_ref=bool(model_refs),
                                        include=include, show_face=ec_show_face)

            run_dir = new_run_dir()
            results = []
            progress = st.progress(0.0, text="准备中...")
            preview = st.empty()
            for i, job in enumerate(jobs, 1):
                progress.progress((i - 1) / len(jobs), text=f"生成第 {i}/{len(jobs)} 张 · {job['name']}...")
                try:
                    # 模特图:整套产品图 + 模特参考图;场景图:只用整套产品图
                    if job["use_model_ref"]:
                        refs = product_refs[:13] + model_refs
                    else:
                        refs = product_refs
                    raw_png = generate_ecom(client, refs, job["prompt"])
                    png = upscale_png(raw_png, scale)
                    name = f"{shop}_{job['name']}"
                    fpath = os.path.join(run_dir, f"{name}.png")
                    with open(fpath, "wb") as fp:
                        fp.write(png)
                    results.append((name, fpath))  # 存路径不存大图,防内存爆掉
                    preview.image(png, caption=f"刚生成:{name}", width=260)
                except Exception as e:
                    st.error(f"{job['name']} 生成失败:{e}")
            progress.progress(1.0, text="完成")
            preview.empty()
            st.session_state["ec_results"] = results
            # 存每张图的生成参数,供"单张重出"抽卡用
            st.session_state["ec_ctx"] = {
                "scale": scale,
                "run_dir": run_dir,
                "jobs": {f"{shop}_{j['name']}": j for j in jobs},
                "product_refs": product_refs,
                "model_refs": model_refs,
            }

    def _regen_ec(idx):
        """只重出电商图第 idx 张(单张计费),其余不动。"""
        ctx = st.session_state.get("ec_ctx")
        results = st.session_state.get("ec_results") or []
        if not ctx or idx >= len(results):
            st.warning("这张图没有保存生成参数(旧批次),请重新生成一批后再抽卡。")
            return
        name = results[idx][0]
        job = ctx["jobs"].get(name)
        if not job:
            st.warning("这张图没有保存生成参数,请重新生成一批后再抽卡。")
            return
        try:
            with st.spinner(f"正在重出 {name}(同参数重新抽一张)..."):
                refs = (ctx["product_refs"][:13] + ctx["model_refs"]) if job["use_model_ref"] \
                    else ctx["product_refs"]
                raw_png = generate_ecom(OpenAI(api_key=api_key), refs, job["prompt"])
                png = upscale_png(raw_png, ctx["scale"])
            fpath = os.path.join(ctx["run_dir"], f"{name}.png")
            with open(fpath, "wb") as fp:
                fp.write(png)
            results[idx] = (name, fpath)
            st.session_state["ec_results"] = results
            st.rerun()
        except Exception as e:
            st.error(f"{name} 重出失败:{e}")

    _render_results(st.session_state.get("ec_results"), f"{shop}_电商图.zip", "ecdl", regen=_regen_ec)


# ===========================================================================
# 标签 3:图片放大(纯本地放大,不调用 API、不耗额度)
# ===========================================================================
def render_upscale():
    st.caption("上传任意图片,选择放大倍数,直接输出放大版(本地高质量放大,不生成、不消耗额度)。")
    files = st.file_uploader("上传要放大的图片(可多张)",
                             type=["png", "jpg", "jpeg", "webp"],
                             accept_multiple_files=True, key="up_files")
    up_scale = st.selectbox("放大倍数", options=["2 倍", "3 倍", "4 倍", "1.5 倍"], index=0, key="up_scale")
    factor = float(up_scale.split()[0])

    if not st.button("🔍 开始放大", type="primary", use_container_width=True, key="up_run"):
        # 持久展示上次结果
        _render_results(st.session_state.get("up_results"), "放大图片.zip", "updl")
        return
    if not files:
        st.warning("请先上传图片。")
        return

    results = []
    run_dir = new_run_dir()
    progress = st.progress(0.0, text="放大中...")
    for i, f in enumerate(files, 1):
        progress.progress((i - 1) / len(files), text=f"放大第 {i}/{len(files)} 张...")
        try:
            data = f.getvalue()
            big = upscale_png(data, factor)
            base = os.path.splitext(getattr(f, "name", f"image_{i}"))[0]
            name = f"{base}_放大{up_scale.split()[0]}x"
            fpath = os.path.join(run_dir, f"{name}.png")
            with open(fpath, "wb") as fp:
                fp.write(big)
            results.append((name, fpath))  # 存路径不存大图,防内存爆掉
        except Exception as e:
            st.error(f"{getattr(f,'name','图片')} 放大失败:{e}")
    progress.progress(1.0, text="完成")
    st.session_state["up_results"] = results
    _render_results(results, "放大图片.zip", "updl")


# ===========================================================================
# 标签 4:历史记录(找回以前生成的批次,不调用 API)
# ===========================================================================
def render_history():
    st.caption("每次生成都会自动按批次存到服务器,在这里随时找回、下载。"
               "注意:服务器重启或重新部署后历史会清空,重要的图请及时打包下载。")
    if not os.path.isdir(OUTPUT_ROOT):
        st.info("还没有历史记录,先去生成一批吧。")
        return
    runs = sorted([d for d in os.listdir(OUTPUT_ROOT)
                   if os.path.isdir(os.path.join(OUTPUT_ROOT, d))], reverse=True)
    if not runs:
        st.info("还没有历史记录,先去生成一批吧。")
        return

    def _label(r):
        # run_20260611_153045 -> 2026-06-11 15:30:45
        try:
            ts = datetime.datetime.strptime(r, "run_%Y%m%d_%H%M%S")
            return ts.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return r

    sel = st.selectbox("选择批次(最新在前)", options=runs, format_func=_label, key="his_run")
    run_dir = os.path.join(OUTPUT_ROOT, sel)
    files = sorted(f for f in os.listdir(run_dir) if f.lower().endswith(".png"))
    if not files:
        st.info("这个批次没有图片。")
        return
    st.write(f"该批次共 {len(files)} 张。")

    # 大图只在点击后加载,且页面上只显示缩略图(防止每次操作都重新推送几十 MB 大图拖垮服务器)
    if st.button("📂 加载这批图片预览", key="his_load"):
        st.session_state["his_loaded"] = sel
    if st.session_state.get("his_loaded") != sel:
        return

    from PIL import Image
    cols = st.columns(4)
    for idx, f in enumerate(files):
        fp = os.path.join(run_dir, f)
        try:
            im = Image.open(fp).convert("RGB")
            im.thumbnail((360, 540))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            with cols[idx % 4]:
                st.image(buf.getvalue(), caption=os.path.splitext(f)[0],
                         use_container_width=True)
        except Exception:
            continue
    # 下载一律走打包(原图全尺寸),不做单张下载按钮,保持页面轻
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            with open(os.path.join(run_dir, f), "rb") as fp2:
                zf.writestr(f, fp2.read())
    st.download_button("📦 下载这批原图(zip)", data=buf.getvalue(),
                       file_name=f"{_label(sel)}_历史批次.zip", mime="application/zip",
                       use_container_width=True, key=f"his_zip_{sel}")


# ===========================================================================
# 页面
# ===========================================================================
st.title("💎 珠宝图片生成器")
check_versions()
require_password()

api_key = get_api_key()
if not api_key:
    st.error("服务器未配置 OPENAI_API_KEY,请联系管理员。")
    st.stop()

buyer_tab, ecom_tab, up_tab, his_tab = st.tabs(
    ["📸 买家秀(生活感)", "💎 电商精修图(高级棚拍)", "🔍 图片放大", "📁 历史记录"])
with buyer_tab:
    render_buyer_show(api_key)
with ecom_tab:
    render_ecommerce(api_key)
with up_tab:
    render_upscale()
with his_tab:
    render_history()
