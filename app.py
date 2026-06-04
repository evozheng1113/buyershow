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
    """按店铺自动加载专属数字模特参考图,最多两张:model_<key> 和 model_<key>2。
    返回 [(name, bytes), ...](可能为空)。"""
    key = SHOP_KEYS.get(shop)
    refs = []
    if not key:
        return refs
    here = os.path.dirname(os.path.abspath(__file__))
    for stem in (f"model_{key}", f"model_{key}2"):
        for ext in _IMG_EXTS:
            path = os.path.join(here, stem + ext)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    refs.append((stem + ext, f.read()))
                break
    return refs


def generate_ecom(client, product, seconds, prompt, quality=ECOM_QUALITY):
    """seconds 是第二参考图列表(0~2 张):产品图在前,模特参考图随后。"""
    images = [(product[0], io.BytesIO(product[1]))]
    for s in (seconds or []):
        images.append((s[0], io.BytesIO(s[1])))
    result = client.images.edit(model=MODEL, image=images, prompt=prompt,
                                size=ECOM_SIZE, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def generate_digital_model(client, shop):
    """为某店铺生成数字模特两张参考图:① 肩颈锁骨(文生图) ② 手部(参考①保持同一人)。"""
    neck_p, hand_p = digital_model_prompts(shop)
    r1 = client.images.generate(model=MODEL, prompt=neck_p, size=ECOM_SIZE, quality=ECOM_QUALITY, n=1)
    neck_png = base64.b64decode(r1.data[0].b64_json)
    r2 = client.images.edit(model=MODEL, image=[("neck.png", io.BytesIO(neck_png))],
                            prompt=hand_p, size=ECOM_SIZE, quality=ECOM_QUALITY, n=1)
    hand_png = base64.b64decode(r2.data[0].b64_json)
    return neck_png, hand_png


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

    mode = st.radio("生成模式", options=["6场景(每场景3张·同一买家)", "无要求(18张各不相同)"],
                    index=0, horizontal=True, key="bs_mode")
    grouped = mode.startswith("6场景")
    st.caption("6 场景 × 每场景 3 张 = 18 张:4 真人佩戴 + 1 手拿 + 1 首饰盒。" if grouped
               else "18 张各不相同:真人佩戴 10 + 手拿 4 + 首饰盒/静物 4,全部不露脸。")

    qcol1, qcol2 = st.columns(2)
    with qcol1:
        n_high = st.slider("其中高画质(high)张数", 0, 18, 4, key="bs_high")
    with qcol2:
        low_tier = st.selectbox("其余张数的画质", options=["medium", "low"], index=0, key="bs_low")

    if not st.button("🚀 开始生成", type="primary", use_container_width=True, key="bs_run"):
        return
    if not jewelry_file or not wearing_file:
        st.warning("请先上传两张图片。")
        return

    if grouped:
        scenes = build_grouped_scenes(jewelry_type=jewelry_type, season=season, env=env)
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
    st.subheader("生成结果(实时更新)")
    cols = st.columns(3)
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
            with cols[(len(results) - 1) % 3]:
                st.image(png, caption=f"{scene['name']} · {q}", use_container_width=True)
                st.download_button("下载这张", data=png, file_name=f"{scene['name']}.png",
                                   mime="image/png", key=f"bsdl_{scene['name']}")
        except Exception as e:
            st.error(f"{scene['name']} 生成失败:{e}")
    progress.progress(1.0, text="完成")
    if results:
        st.success(f"成功生成 {len(results)} 张。")
        zip_download(results, "buyer_shows.zip")


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

    # 该店铺的专属数字模特(自动从仓库加载,最多两张;也允许临时上传覆盖)
    shop_models = load_shop_models(shop)
    if shop_models:
        st.success(f"已自动加载【{shop}】的专属数字模特(共 {len(shop_models)} 张参考)。")
    else:
        st.info(f"未检测到【{shop}】的专属数字模特(model_{SHOP_KEYS[shop]} / model_{SHOP_KEYS[shop]}2)。"
                "可用下面的按钮生成,或临时上传一张。")

    # 一键生成该店数字模特(脖子图 + 手图),下载后命名 model_X / model_X2 传到仓库即固定
    with st.expander("🧑‍🎨 为本店生成数字模特(脖子图 + 手图)"):
        st.caption(f"按【{shop}】的肤色/气质生成一个虚拟模特(无首饰、不露脸)。"
                   f"下载后改名为 model_{SHOP_KEYS[shop]} 和 model_{SHOP_KEYS[shop]}2,传到仓库即长期固定使用。")
        if st.button("生成数字模特(2 张)", key="ec_gen_model"):
            try:
                with st.spinner("正在生成数字模特..."):
                    neck_png, hand_png = generate_digital_model(OpenAI(api_key=api_key), shop)
                k = SHOP_KEYS[shop]
                mc1, mc2 = st.columns(2)
                with mc1:
                    st.image(neck_png, caption=f"肩颈图 → 存为 model_{k}.png", use_container_width=True)
                    st.download_button("下载肩颈图", data=neck_png, file_name=f"model_{k}.png",
                                       mime="image/png", key="ec_dl_neck")
                with mc2:
                    st.image(hand_png, caption=f"手部图 → 存为 model_{k}2.png", use_container_width=True)
                    st.download_button("下载手部图", data=hand_png, file_name=f"model_{k}2.png",
                                       mime="image/png", key="ec_dl_hand")
                st.info("满意就下载这两张,改成上面的文件名传到 GitHub 仓库;不满意可再点一次重新生成。")
            except Exception as e:
                st.error(f"生成失败:{e}")

    c1, c2 = st.columns(2)
    with c1:
        prod_file = st.file_uploader("① 白底产品图(必传,决定珠宝款式)",
                                     type=["png", "jpg", "jpeg", "webp"], key="ec_prod")
        if prod_file:
            st.image(prod_file, caption="产品真值图", use_container_width=True)
    with c2:
        model_file = st.file_uploader("② 临时模特参考图(可选,覆盖本店默认模特)",
                                      type=["png", "jpg", "jpeg", "webp"], key="ec_model")
        if model_file:
            st.image(model_file, caption="临时模特参考(本次覆盖)", use_container_width=True)

    st.caption("每款固定 6 张:3 模特图(不同角度) + 3 场景图(色调渐变 / 石材 / 道具)。")

    if not st.button("🚀 生成电商图", type="primary", use_container_width=True, key="ec_run"):
        return
    if not prod_file:
        st.warning("请先上传白底产品图。")
        return

    client = OpenAI(api_key=api_key)
    product = to_named_bytes(prod_file, "product.png")
    # 优先用临时上传(单张),否则用该店铺的专属数字模特(可两张)
    model_refs = [to_named_bytes(model_file, "model.png")] if model_file else shop_models
    jobs = build_ecommerce_jobs(shop, jtype, has_model_ref=bool(model_refs))

    run_dir = new_run_dir()
    results = []
    progress = st.progress(0.0, text="准备中...")
    st.subheader("生成结果(实时更新)")
    cols = st.columns(3)
    for i, job in enumerate(jobs, 1):
        progress.progress((i - 1) / len(jobs), text=f"生成第 {i}/{len(jobs)} 张 · {job['name']}...")
        try:
            seconds = model_refs if job["use_model_ref"] else []
            png = generate_ecom(client, product, seconds, job["prompt"])
            name = f"{shop}_{job['name']}"
            results.append((name, png))
            with open(os.path.join(run_dir, f"{name}.png"), "wb") as fp:
                fp.write(png)
            with cols[(len(results) - 1) % 3]:
                st.image(png, caption=name, use_container_width=True)
                st.download_button("下载这张", data=png, file_name=f"{name}.png",
                                   mime="image/png", key=f"ecdl_{name}")
        except Exception as e:
            st.error(f"{job['name']} 生成失败:{e}")
    progress.progress(1.0, text="完成")
    if results:
        st.success(f"成功生成 {len(results)} 张。")
        zip_download(results, f"{shop}_电商图.zip")


# ===========================================================================
# 页面
# ===========================================================================
st.title("💎 珠宝图片生成器")
require_password()

api_key = get_api_key()
if not api_key:
    st.error("服务器未配置 OPENAI_API_KEY,请联系管理员。")
    st.stop()

buyer_tab, ecom_tab = st.tabs(["📸 买家秀(生活感)", "💎 电商精修图(高级棚拍)"])
with buyer_tab:
    render_buyer_show(api_key)
with ecom_tab:
    render_ecommerce(api_key)
