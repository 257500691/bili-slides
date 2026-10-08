#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BV 号 / 视频文件  ->  幻灯片 PDF

只做一件事：把视频里出现过的每一页幻灯片截出来，合成一份图片型 PDF。
不转录、不做笔记、不做 OCR、不生成任何别的文件。

用法:
  python bili_slides.py BV1xx411c7mD
  python bili_slides.py "https://www.bilibili.com/video/BV1xx411c7mD?p=3"
  python bili_slides.py --video 本地文件.mp4
  python bili_slides.py BV1xx411c7mD --cookies chrome     # 1080P 需要登录态时
"""
import argparse
import glob
import os
import re
import shutil
import sys
import time

WORK = os.path.dirname(os.path.abspath(__file__))
LIBS = os.path.join(WORK, "libs")
if os.path.isdir(LIBS):
    sys.path.insert(0, LIBS)

import numpy as np
from PIL import Image
import cv2

SENSITIVITY = {"high": 0.008, "normal": 0.020, "low": 0.050}


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def resolve_input(s):
    s = s.strip().strip('"')
    if re.fullmatch(r"[Bb][Vv][0-9A-Za-z]{10}", s):
        return "https://www.bilibili.com/video/" + s
    if re.fullmatch(r"av\d+", s, re.I):
        return "https://www.bilibili.com/video/" + s.lower()
    return s


def download(url, outdir, page=None, cookies=None, quiet=False,
             force_h264=False):
    from yt_dlp import YoutubeDL

    if page:
        url = url + ("&" if "?" in url else "?") + "p=%d" % page
    os.makedirs(outdir, exist_ok=True)

    opts = {
        # 只要画面，不要音频 -> 单条流，不需要 ffmpeg 混流
        "format": "bv*[vcodec^=avc1]/b" if force_h264 else "bv*/b",
        # 优先 H.264：OpenCV 一定能解；AV1/HEVC 视解码器而定
        "format_sort": ["vcodec:h264", "res", "tbr"],
        "outtmpl": os.path.join(outdir, "%(id)s_p%(p)s.%(ext)s"),
        "noplaylist": True,
        "quiet": quiet,
        "noprogress": quiet,
        "no_warnings": quiet,
        "retries": 3,
        "fragment_retries": 5,
    }
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        cands = [p for p in glob.glob(os.path.join(outdir, "*"))
                 if os.path.splitext(p)[1].lower() in
                 (".mp4", ".mkv", ".flv", ".webm", ".m4s", ".ts")]
        if not cands:
            raise RuntimeError("下载后找不到视频文件")
        path = max(cands, key=os.path.getmtime)
    return path, info


# --------------------------------------------------------------------------
# 帧比较
# --------------------------------------------------------------------------
def shrink(frame, width):
    h = max(1, int(round(frame.shape[0] * width / frame.shape[1])))
    return cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)


def block_map(a, b, block):
    """返回每块的平均绝对差 (rows x cols)，float32"""
    h, w = a.shape[:2]
    rows, cols = h // block, w // block
    if rows < 1 or cols < 1:
        return np.zeros((1, 1), np.float32)
    a = a[: rows * block, : cols * block].astype(np.int16)
    b = b[: rows * block, : cols * block].astype(np.int16)
    d = np.abs(a - b).mean(axis=2)
    return d.reshape(rows, block, cols, block).mean(axis=(1, 3)).astype(np.float32)


def ratio_of(dmap, block_delta, mask):
    changed = dmap > block_delta
    if mask is not None and mask.shape == changed.shape:
        changed = changed[~mask]
    total = changed.size
    if total == 0:
        return 0.0
    return float(changed.sum()) / float(total)


def same_slide(a, b, thr=8.0, side_ratio=0.15, max_side=0.35, max_mae=16.0):
    """判断两张帧是不是"同一页幻灯片"。

    两条路：
    1) 整体差异足够小；
    2) 差异是"单向"的——只有内容被加进来、或只有内容被抹掉，
       而几乎没有反向变化。这对应屏幕批注、高亮框、逐条展开的中间状态。
       真正的翻页一定是旧内容消失 + 新内容出现，两个方向都有变化。

    实测：同一页的批注状态 min/max ≈ 0.00，而真翻页页对在 0.32~1.00。
    """
    d = b - a
    mae = float(np.abs(d).mean())
    if mae < thr:
        return True
    if mae >= max_mae:
        return False
    add = float((d > 25).mean())
    rem = float((-d > 25).mean())
    mx, mn = max(add, rem), min(add, rem)
    if mx < 0.01:
        return True
    return (mn / mx) < side_ratio and mx <= max_side


# --------------------------------------------------------------------------
# 校准：找出「一直在动的区域」（鼠标 / 摄像头小窗 / 播放器控件）
# --------------------------------------------------------------------------
def calibrate(path, sample_fps, work_w, block, block_delta,
              window_s=40.0, active_frac=0.5, max_mask=0.35,
              default_thr=0.04, log=print):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("打不开视频: " + path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = (n / fps) if n > 0 else 0.0
    if n <= 0:
        cap.release()
        return None, default_thr

    start = min(dur * 0.15, max(dur - window_s, 0.0))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * fps))
    step = max(1, int(round(fps / sample_fps)))

    frames, i = [], 0
    limit = int(window_s * sample_fps)
    while len(frames) < limit:
        if not cap.grab():
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if ok and fr is not None:
                frames.append(shrink(fr, work_w))
        i += 1
    cap.release()

    if len(frames) < 8:
        return None, default_thr

    dmaps = [block_map(frames[k], frames[k + 1], block) for k in range(len(frames) - 1)]
    rows, cols = dmaps[0].shape

    # 一直在动的块 = 超过 active_frac 的相邻帧对里都变了
    hits = np.zeros((rows, cols), np.float32)
    for d in dmaps:
        hits += (d > block_delta).astype(np.float32)
    mask = (hits / len(dmaps)) > active_frac
    if mask.sum() == 0 or mask.sum() > max_mask * mask.size:
        mask = None
    else:
        log("  屏蔽 %d/%d 个区块（鼠标/小窗一类常动区域）"
            % (int(mask.sum()), mask.size))

    return mask, default_thr


# --------------------------------------------------------------------------
# 主扫描：检测翻页
# --------------------------------------------------------------------------
def scan(path, sample_fps, work_w, block, block_delta, threshold, mask,
         log=print):
    """顺序扫描整段视频，返回每一页起始的帧号。

    比较对象始终是「上一张被采纳的幻灯片」而不是上一帧：
    这样同页上的鼠标移动、局部动画、压缩噪声都不会累积成翻页。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("打不开视频: " + path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(round(fps / sample_fps)))

    keep, prev, i = [], None, 0
    while True:
        if not cap.grab():
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if ok and fr is not None:
                cur = shrink(fr, work_w)
                if prev is None:
                    keep.append(i)
                    prev = cur
                else:
                    r = ratio_of(block_map(cur, prev, block), block_delta, mask)
                    if r > threshold:
                        keep.append(i)
                        prev = cur
        i += 1
    cap.release()
    return keep, fps, n


# --------------------------------------------------------------------------
# 导出：在每一页的停留区间里取源分辨率干净帧，并去掉重复
# --------------------------------------------------------------------------
def export(path, keep, fps, n, outdir, max_w, dedupe_mae=8.0, log=print):
    """导出每一页，并做两件清理：

    1) 全局去重：拿候选和「之前保留过的所有页」比，而不只是比上一页。
       讲师来回翻页、或同一页出现多个批注/展开状态时，都能并成一页。
    2) 画面类型过滤：整段视频若整体是浅色数字幻灯片，就丢掉明显不属于
       这一类（实拍、摄像头、桌面录屏）的帧。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("打不开视频: " + path)

    cand_dir = os.path.join(outdir, "_cand")
    shutil.rmtree(cand_dir, ignore_errors=True)
    os.makedirs(cand_dir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, "slide-*.png")):
        os.remove(f)

    cands = []
    for k, idx in enumerate(keep):
        nxt = keep[k + 1] if k + 1 < len(keep) else n
        dwell = max(1, nxt - idx)
        if dwell < int(0.8 * fps) and k + 1 < len(keep):
            continue                      # 太短，多半是过渡帧
        pos = min(int(idx + dwell * 0.45), n - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, fr = cap.read()
        if not ok or fr is None:
            continue

        im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        if im.width > max_w:
            im = im.resize((max_w, int(round(im.height * max_w / im.width))),
                           Image.LANCZOS)
        gray = im.convert("L")
        arr = np.asarray(gray, dtype=np.float32)
        if float(arr.std()) < 3.0:
            continue                      # 纯黑/纯白，不是幻灯片
        small = np.asarray(gray.resize((320, 240), Image.BILINEAR),
                           dtype=np.float32)
        f_path = os.path.join(cand_dir, "cand-%04d.png" % len(cands))
        im.save(f_path)
        cands.append({
            "file": f_path, "t": idx / fps, "gray": small,
            "bright": float((arr > 200).mean()),
        })
    cap.release()

    if not cands:
        shutil.rmtree(cand_dir, ignore_errors=True)
        return []

    brights = np.array([c["bright"] for c in cands])
    light_deck = float(np.median(brights)) > 0.45
    dropped_theme = 0

    written = []
    for c in cands:
        if light_deck and c["bright"] < 0.30:
            dropped_theme += 1
            continue
        if any(same_slide(w["gray"], c["gray"], dedupe_mae)
               for w in written):
            continue
        written.append(c)

    if dropped_theme:
        log("      丢掉 %d 帧非幻灯片画面（实拍/桌面一类）" % dropped_theme)
    log("      去掉 %d 帧重复/同页多状态" % (len(cands) - dropped_theme - len(written)))

    pages = []
    for i, c in enumerate(written, 1):
        out = os.path.join(outdir, "slide-%03d.png" % i)
        shutil.copyfile(c["file"], out)
        pages.append((out, c["t"]))
    shutil.rmtree(cand_dir, ignore_errors=True)
    return pages


def build_pdf(pages, out_pdf, log=print):
    if not pages:
        raise RuntimeError("没有截到任何幻灯片")
    # 懒加载：Pillow 会逐张解码写入，几百页也不会一次性吃满内存
    imgs = [Image.open(p) for p, _ in pages]
    imgs[0].save(out_pdf, "PDF", save_all=True, append_images=imgs[1:],
                 resolution=150.0)
    return len(imgs)


def merge_pdfs(part_pdfs, out_pdf, titles, log=print):
    """把各分 P 的 PDF 合成一个，并按分 P 建书签。"""
    from pypdf import PdfWriter, PdfReader
    writer = PdfWriter()
    for path, title in zip(part_pdfs, titles):
        start = len(writer.pages)
        for pg in PdfReader(path).pages:
            writer.add_page(pg)
        writer.add_outline_item(title, start)
    with open(out_pdf, "wb") as fh:
        writer.write(fh)
    return len(writer.pages)


def list_parts(url, cookies=None):
    """返回 (合集标题, [分P链接...])；不是合集就返回 (None, None)。"""
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    if info.get("_type") != "playlist":
        return None, None
    urls = [e.get("url") for e in (info.get("entries") or [])
            if e and e.get("url")]
    return info.get("title"), (urls or None)


def extract_pages(video, out_dir, args, thr, log=print):
    """校准 + 扫描 + 导出，返回这一集的所有幻灯片。"""
    os.makedirs(out_dir, exist_ok=True)
    mask, _ = calibrate(video, args.sample_fps, 320, 8, 14,
                        default_thr=thr, log=log)
    keep, fps, n = scan(video, args.sample_fps, 320, 8, 14, thr, mask, log=log)
    log("      采样 %d 点，检测到 %d 处变化（时长 %.1f 分钟）"
        % (int(n / fps * args.sample_fps), len(keep), n / fps / 60.0))
    return export(video, keep, fps, n, out_dir, args.max_width,
                  dedupe_mae=args.dedupe_mae, log=log)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="B站视频 -> 幻灯片 PDF（只输出幻灯片）")
    ap.add_argument("target", nargs="?",
                    help="BV 号 / av 号 / B 站链接 / 本地视频路径")
    ap.add_argument("--video", help="直接用本地视频文件")
    ap.add_argument("-p", "--page", type=int, help="合集里的第几个分 P")
    ap.add_argument("-o", "--out", default="slides_out", help="输出目录")
    ap.add_argument("--name", help="输出的 PDF 文件名")
    ap.add_argument("--cookies", choices=["chrome", "edge", "firefox", "brave"],
                    help="用浏览器登录态下载（1080P 及以上通常需要）")
    ap.add_argument("--sensitivity", choices=list(SENSITIVITY), default="normal",
                    help="翻页灵敏度：high 多截不漏 / low 少截更干净")
    ap.add_argument("--threshold", type=float, help="直接指定阈值，覆盖灵敏度")
    ap.add_argument("--sample-fps", type=float, default=2.0,
                    help="每秒比对多少帧，默认 2")
    ap.add_argument("--max-width", type=int, default=1920,
                    help="幻灯片图片最大宽度，默认 1920")
    ap.add_argument("--dedupe-mae", type=float, default=8.0,
                    help="判为同一页的像素差异阈值，默认 8.0。调小到 2 会把"
                         "同一页的批注/展开中间状态也各留一页")
    ap.add_argument("--all-parts", action="store_true",
                    help="整个合集/多分 P 全跑，最后合成一个 PDF")
    ap.add_argument("--keep-images", action="store_true",
                    help="保留每页的 PNG（默认只留最终的 PDF）")
    ap.add_argument("--keep-video", action="store_true",
                    help="保留下载的视频文件")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def log(*a):
        if not args.quiet:
            print(*a, flush=True)

    video = args.video
    info = None
    dl_dir = None
    target = None

    if not video:
        if not args.target:
            ap.error("给一个 BV 号/链接，或用 --video 指定本地文件")
        target = resolve_input(args.target)
        if os.path.exists(target):
            video = target

    thr = args.threshold if args.threshold is not None \
        else SENSITIVITY[args.sensitivity]

    # 解码自检：编码太新（AV1/HEVC）而本机解不开时，自动换 H.264 重下
    def can_decode(p):
        c = cv2.VideoCapture(p)
        ok, _ = c.read()
        c.release()
        return ok

    # ---------------- 合集模式：全部跑完，合成一个 PDF ----------------
    if args.all_parts and not video:
        col_title, urls = list_parts(target, cookies=args.cookies)
        if not urls:
            log("这个链接不是合集/多分 P，按单集处理")
        else:
            log("合集「%s」共 %d 个分 P，逐个提取后合成一个 PDF"
                % (col_title, len(urls)))
            dl_root = os.path.join(args.out, "_video")
            part_root = os.path.join(args.out, "_parts")
            part_pdfs, titles, failed, total_pages = [], [], [], 0
            t_all = time.time()
            for i, pu in enumerate(urls, 1):
                log("")
                log("===== [%d/%d] %s =====" % (i, len(urls), pu))
                t0 = time.time()
                vdir = os.path.join(dl_root, "p%02d" % i)
                pdir = os.path.join(part_root, "p%02d" % i)
                try:
                    vpath, info = download(pu, vdir, cookies=args.cookies,
                                           quiet=True)
                    log("      已下载 %.1f MB" % (os.path.getsize(vpath) / 1048576))
                    if not can_decode(vpath):
                        log("      这个流解不开，改用 H.264 重下")
                        vpath, info = download(pu, vdir, cookies=args.cookies,
                                               quiet=True, force_h264=True)
                    if not can_decode(vpath):
                        raise RuntimeError("编码解不开（AV1/HEVC）")
                    pages = extract_pages(vpath, pdir, args, thr, log=log)
                    if not pages:
                        raise RuntimeError("没截到幻灯片")
                    pp = os.path.join(pdir, "part.pdf")
                    build_pdf(pages, pp)
                    part_pdfs.append(pp)
                    titles.append(info.get("title") or ("P%02d" % i))
                    total_pages += len(pages)
                    log("      本集 %d 页，用时 %.0f 秒"
                        % (len(pages), time.time() - t0))
                except Exception as e:
                    failed.append((i, str(e)[:120]))
                    log("      失败：%s" % str(e)[:200])
                finally:
                    if not args.keep_video:
                        shutil.rmtree(vdir, ignore_errors=True)

            if not part_pdfs:
                raise SystemExit("所有分 P 都失败了")

            name = args.name or ("%s_全%dP" % (col_title or "合集", len(urls)))
            name = re.sub(r'[\\/:*?"<>|]', "_", name)[:100]
            pdf = os.path.join(args.out, name + ".pdf")
            n_total = merge_pdfs(part_pdfs, pdf, titles, log=log)
            log("")
            log("合并完成：%d 集 / %d 页 -> %s" % (len(part_pdfs), n_total, pdf))
            log("总用时 %.1f 分钟" % ((time.time() - t_all) / 60.0))
            if failed:
                log("以下分 P 失败，可单独重跑：")
                for i, msg in failed:
                    log("  P%02d: %s" % (i, msg))
            if args.keep_images:
                log("每页 PNG 保留在", part_root)
            else:
                shutil.rmtree(part_root, ignore_errors=True)
            return

    # ---------------- 单集模式 ----------------
    if not video:
        dl_dir = os.path.join(args.out, "_video")
        log("[1/4] 下载视频…（只下画面流，不需要 ffmpeg）")
        video, info = download(target, dl_dir, page=args.page,
                               cookies=args.cookies, quiet=args.quiet)
        log("      ->", os.path.basename(video))
    elif not os.path.exists(video):
        raise SystemExit("找不到文件: " + video)

    if not can_decode(video):
        if dl_dir and target:
            log("      这个流的编码本机解不开，改用 H.264 重新下载…")
            video, info = download(target, dl_dir, page=args.page,
                                   cookies=args.cookies, quiet=args.quiet,
                                   force_h264=True)
        if not can_decode(video):
            raise SystemExit(
                "解不开这个视频的编码（AV1/HEVC）。请换一档清晰度，"
                "或先用其它工具转成 H.264 再跑。")

    log("[2/4] 校准（找常动区域 + 定阈值）…")
    log("      阈值 %.3f" % thr)
    log("[3/4] 扫描翻页…")
    log("[4/4] 取源分辨率干净帧 + 去重…")
    pages = extract_pages(video, os.path.join(args.out, "_slides"), args, thr,
                          log=log)

    title = args.name
    if not title:
        if info and info.get("title"):
            title = re.sub(r'[\\/:*?"<>|]', "_", info["title"])[:80]
        else:
            title = os.path.splitext(os.path.basename(video))[0]
        if args.page:
            title += "_p%d" % args.page
    pdf = os.path.join(args.out, title + ".pdf")
    n_page = build_pdf(pages, pdf, log=log)

    if not args.keep_video and dl_dir and os.path.isdir(dl_dir):
        shutil.rmtree(dl_dir, ignore_errors=True)
    elif dl_dir:
        log("      视频保留在", dl_dir)

    log("")
    log("完成：%d 页幻灯片 -> %s" % (n_page, pdf))
    log("（图片型 PDF，不能 Ctrl+F，这是「只要幻灯片」的必然结果）")


if __name__ == "__main__":
    main()
