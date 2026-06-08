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

# 每次生成自动保存到脚本同目录下的 outputs/ 文件夹,按时间分批
OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

from buyer_show import (
    FIDELITY_RULES,
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


def new_run_dir():
    d = os.path.join(OUTPUT_ROOT, datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def zip_download(results, fname):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, png in results:
            zf.writestr(f"{name}.png", png)
    st.download_button("📦 打包下载全部", data=buf.getvalue(),
                       file_name=fname, mime="application/zip",
                       use_container_width=True)


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


def generate_one(client, jewelry, second, scene, quality=QUALITY):
    """买家秀单张:拼买家秀铁律(iPhone 真实感/不露脸)。"""
    full_prompt = FIDELITY_RULES + "\n【本张场景】" + scene["prompt"]
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
        env = st.selectbox("场景环境", options=["不限", "室内", "户外"], index=0, key="bs_env")

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
            results, group_base = [], {}
            progress = st.progress(0.0, text="准备中...")
            preview = st.empty()
            for i, scene in enumerate(scenes, 1):
                q = qualities[i - 1]
                progress.progress((i - 1) / len(scenes), text=f"生成第 {i}/{len(scenes)} 张({q})...")
                try:
                    if scene.get("ref") == "base":
                        base_png = group_base.get(scene.get("group"))
                        second = ("base.png", base_png) if base_png else wearing
                    else:
                        second = pick_second_ref(scene, wearing, boxes)
                    png = generate_one(client, jewelry, second, scene, quality=q)
                    if scene.get("var") == 0 and scene.get("group") is not None:
                        group_base[scene["group"]] = png
                    results.append((scene["name"], png))
                    with open(os.path.join(run_dir, f"{scene['name']}.png"), "wb") as fp:
                        fp.write(png)
                    preview.image(png, caption=f"刚生成:{scene['name']} · {q}", width=260)
                except Exception as e:
                    st.error(f"{scene['name']} 生成失败:{e}")
            progress.progress(1.0, text="完成")
            preview.empty()
            st.session_state["bs_results"] = results  # 存起来,点下载不丢

    # 持久展示(从会话状态,点任意下载/刷新后都还在)
    _render_results(st.session_state.get("bs_results"), "buyer_shows.zip", "bsdl")


def _render_results(results, zip_name, key_prefix):
    if not results:
        return
    st.success(f"成功生成 {len(results)} 张。")
    cols = st.columns(3)
    for idx, (name, png) in enumerate(results):
        with cols[idx % 3]:
            st.image(png, caption=name, use_container_width=True)
            st.download_button("下载这张", data=png, file_name=f"{name}.png",
                               mime="image/png", key=f"{key_prefix}_{name}")
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
                             options=["项链", "戒指", "手链", "手镯", "耳饰", "自动判断"],
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
            jobs = build_ecommerce_jobs(shop, jtype, has_model_ref=bool(model_refs), include=include)

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
                    results.append((name, png))
                    with open(os.path.join(run_dir, f"{name}.png"), "wb") as fp:
                        fp.write(png)
                    preview.image(png, caption=f"刚生成:{name}", width=260)
                except Exception as e:
                    st.error(f"{job['name']} 生成失败:{e}")
            progress.progress(1.0, text="完成")
            preview.empty()
            st.session_state["ec_results"] = results

    _render_results(st.session_state.get("ec_results"), f"{shop}_电商图.zip", "ecdl")


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
    progress = st.progress(0.0, text="放大中...")
    for i, f in enumerate(files, 1):
        progress.progress((i - 1) / len(files), text=f"放大第 {i}/{len(files)} 张...")
        try:
            data = f.getvalue()
            big = upscale_png(data, factor)
            base = os.path.splitext(getattr(f, "name", f"image_{i}"))[0]
            results.append((f"{base}_放大{up_scale.split()[0]}x", big))
        except Exception as e:
            st.error(f"{getattr(f,'name','图片')} 放大失败:{e}")
    progress.progress(1.0, text="完成")
    st.session_state["up_results"] = results
    _render_results(results, "放大图片.zip", "updl")


# ===========================================================================
# 页面
# ===========================================================================
st.title("💎 珠宝图片生成器")
require_password()

api_key = get_api_key()
if not api_key:
    st.error("服务器未配置 OPENAI_API_KEY,请联系管理员。")
    st.stop()

buyer_tab, ecom_tab, up_tab = st.tabs(
    ["📸 买家秀(生活感)", "💎 电商精修图(高级棚拍)", "🔍 图片放大"])
with buyer_tab:
    render_buyer_show(api_key)
with ecom_tab:
    render_ecommerce(api_key)
with up_tab:
    render_upscale()
