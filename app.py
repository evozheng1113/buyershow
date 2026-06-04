#!/usr/bin/env python3
"""
首饰买家秀生成器 · 网页版 (Streamlit)
=====================================
同事打开网址 -> 上传两张图 -> 点生成 -> 预览并下载多张买家秀。
不需要装任何东西,API key 集中放在服务器(st.secrets / 环境变量),用户不接触。

本地启动:
  pip install -r requirements.txt
  export OPENAI_API_KEY="sk-..."
  streamlit run app.py

部署后,所有人共用服务器上的同一个 key,费用走你这一个账号。
"""

import base64
import datetime
import io
import os
import random
import zipfile

import streamlit as st
from openai import OpenAI

# 每次生成自动保存到脚本同目录下的 outputs/ 文件夹,按时间分批,永不丢失
OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

# 复用命令行脚本里的场景库与约束规则,避免重复维护
from buyer_show import (
    FIDELITY_RULES,
    MODEL,
    SIZE,
    QUALITY,
    JEWELRY_TYPES,
    build_scene_pool,
)

st.set_page_config(page_title="首饰买家秀生成器", page_icon="💍", layout="wide")


def get_api_key():
    """优先从 st.secrets 读 key,其次环境变量。用户界面不暴露 key。"""
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:
        pass
    return os.getenv("OPENAI_API_KEY")


def get_app_password():
    """访问口令:优先 st.secrets,其次环境变量。没设置则不启用口令。"""
    try:
        if "APP_PASSWORD" in st.secrets:
            return st.secrets["APP_PASSWORD"]
    except Exception:
        pass
    return os.getenv("APP_PASSWORD")


def require_password():
    """有设口令就拦一道;输对了记住,本次会话不再问。没设口令则直接放行。"""
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
    """把 Streamlit 上传对象转成 (filename, bytes) 供 API 使用。"""
    data = uploaded.getvalue()
    name = getattr(uploaded, "name", fallback_name) or fallback_name
    return (name, data)


# 指定的两款首饰盒参考图(放在仓库根目录,与 app.py 同级)
# 文件名用 box_black / box_burgundy,后缀 png/jpg/jpeg/webp 都可以
BOX_FILES = {"box_black": "box_black", "box_burgundy": "box_burgundy"}
_IMG_EXTS = [".png", ".jpg", ".jpeg", ".webp"]


def load_box_images():
    """启动时读取两款盒子图(若存在),返回 {ref: (filename, bytes)}。"""
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
    """单场景生成,返回 PNG bytes。second 是第二张参考图 (name, bytes) 或 None。"""
    full_prompt = FIDELITY_RULES + "\n【本张场景】" + scene["prompt"]
    images = [(jewelry[0], io.BytesIO(jewelry[1]))]
    if second is not None:
        images.append((second[0], io.BytesIO(second[1])))
    result = client.images.edit(
        model=MODEL,
        image=images,
        prompt=full_prompt,
        size=SIZE,
        quality=quality,
        n=1,
    )
    return base64.b64decode(result.data[0].b64_json)


def pick_second_ref(scene, wearing, boxes):
    """按场景的 ref 选第二张参考图:佩戴图 / 盒子图 / 无。"""
    ref = scene.get("ref", "wearing")
    if ref == "wearing":
        return wearing
    if ref in boxes:           # box_black / box_burgundy 且文件存在
        return boxes[ref]
    if ref in BOX_FILES:       # 指定了盒子但文件没上传 -> 退化成只用首饰图(靠文字描述)
        return None
    return None                # ref == "none"


def assign_qualities(scenes, n_high, low_tier):
    """高画质优先给细节最重要的镜头(首饰盒>手拿>佩戴),其余用 low_tier。"""
    priority = {"box": 0, "held": 1, "worn": 2}
    order = sorted(
        range(len(scenes)),
        key=lambda i: priority.get(scenes[i]["name"].split("_")[-1], 3),
    )
    high_idx = set(order[:n_high])
    return ["high" if i in high_idx else low_tier for i in range(len(scenes))]


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
st.title("💍 首饰买家秀生成器")
st.caption("上传白底首饰图 + 模特佩戴图,自动生成多张不同生活场景的真实买家秀。")

require_password()  # 访问口令(在 Secrets 里设了 APP_PASSWORD 才会启用)

api_key = get_api_key()
if not api_key:
    st.error("服务器未配置 OPENAI_API_KEY,请联系管理员。")
    st.stop()

col1, col2 = st.columns(2)
with col1:
    jewelry_file = st.file_uploader(
        "① 白底首饰图(决定款式 / 细节)", type=["png", "jpg", "jpeg", "webp"]
    )
    if jewelry_file:
        st.image(jewelry_file, caption="首饰真值图", use_container_width=True)
with col2:
    wearing_file = st.file_uploader(
        "② 模特佩戴图(决定佩戴比例 / 大小)", type=["png", "jpg", "jpeg", "webp"]
    )
    if wearing_file:
        st.image(wearing_file, caption="佩戴比例参照图", use_container_width=True)

jewelry_type = st.selectbox(
    "首饰类型(决定戴在哪个部位,务必选对)",
    options=["手链", "项链", "耳钉/耳环", "戒指", "手镯", "脚链", "自动判断"],
    index=0,
)

scol1, scol2 = st.columns(2)
with scol1:
    season = st.selectbox("季节(决定穿搭)", options=["不限", "夏天", "冬天"], index=0)
with scol2:
    env = st.selectbox("场景环境", options=["不限", "室内", "户外"], index=0)

st.caption("每个款式固定生成 18 张:真人佩戴 10 张 + 手拿 4 张 + 首饰盒/静物 4 张,全部不露脸。")

qcol1, qcol2 = st.columns(2)
with qcol1:
    n_high = st.slider("其中高画质(high)张数", min_value=0, max_value=18, value=4,
                       help="高画质优先给首饰盒/手拿特写等细节镜头,其余用下面的画质")
with qcol2:
    low_tier = st.selectbox("其余张数的画质", options=["medium", "low"], index=0)

run = st.button("🚀 开始生成", type="primary", use_container_width=True)

if run:
    if not jewelry_file or not wearing_file:
        st.warning("请先上传两张图片。")
        st.stop()

    scenes = build_scene_pool(jewelry_type=jewelry_type, season=season, env=env)  # 固定 18 张
    qualities = assign_qualities(scenes, min(n_high, len(scenes)), low_tier)
    client = OpenAI(api_key=api_key)

    jewelry = to_named_bytes(jewelry_file, "jewelry.png")
    wearing = to_named_bytes(wearing_file, "wearing.png")
    boxes = load_box_images()  # 两款盒子参考图(若已上传到仓库)
    if not boxes:
        st.warning("未检测到盒子参考图(box_black.png / box_burgundy.png),"
                   "首饰盒场景将退化为按文字描述生成。把两张盒子图传到仓库即可严格还原。")

    # 本批的保存文件夹(按时间命名,自动留底)
    run_dir = os.path.join(OUTPUT_ROOT, datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    st.info(f"📁 本批图片会自动保存到:{run_dir}")

    results = []  # (name, png_bytes)
    progress = st.progress(0.0, text="准备中...")

    # 边生成边显示:每出一张立刻渲染,不用等全部跑完
    st.subheader("生成结果(实时更新)")
    cols = st.columns(3)
    for i, scene in enumerate(scenes, 1):
        q = qualities[i - 1]
        progress.progress((i - 1) / len(scenes), text=f"生成第 {i}/{len(scenes)} 张({q})...")
        try:
            second = pick_second_ref(scene, wearing, boxes)
            png = generate_one(client, jewelry, second, scene, quality=q)
            results.append((scene["name"], png))
            # 立刻写盘保存
            with open(os.path.join(run_dir, f"{scene['name']}.png"), "wb") as fp:
                fp.write(png)
            with cols[(len(results) - 1) % 3]:
                st.image(png, caption=f"{scene['name']} · {q}", use_container_width=True)
                st.download_button(
                    "下载这张", data=png, file_name=f"{scene['name']}.png",
                    mime="image/png", key=f"dl_{scene['name']}",
                )
        except Exception as e:
            st.error(f"{scene['name']} 生成失败:{e}")
    progress.progress(1.0, text="完成")

    if results:
        st.success(f"成功生成 {len(results)} 张。")

        # 打包 zip 一键下载全部
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, png in results:
                zf.writestr(f"{name}.png", png)
        st.download_button(
            "📦 打包下载全部", data=buf.getvalue(),
            file_name="buyer_shows.zip", mime="application/zip",
            use_container_width=True,
        )
