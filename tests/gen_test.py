#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成合成"网课"视频用于自测。

  python gen_test.py distinct   # 6 页明显不同的幻灯片 + 常动的摄像头小窗
  python gen_test.py static     # 全程 1 页不变 + 常动的摄像头小窗（考验会不会灌爆）
  python gen_test.py subtle     # 6 页只改一行小字（难点case）
"""
import sys

import cv2
import numpy as np

W, H, FPS = 640, 360, 10
rng = np.random.default_rng(0)


def bubble(img, t):
    """右下角一直在变的"摄像头小窗" """
    img[H - 130:H - 40, W - 190:W - 30] = rng.integers(
        40, 215, (90, 160, 3), dtype=np.uint8)


def make(mode, seconds, n_slide, slide_fn, out):
    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    dwell = seconds / n_slide
    for f in range(seconds * FPS):
        k = min(int(f / (dwell * FPS)), n_slide - 1)
        img = np.full((H, W, 3), 245, np.uint8)
        slide_fn(img, k)
        bubble(img, f)
        writer.write(img)
    writer.release()
    print("wrote", out)


def distinct(img, k):
    cv2.rectangle(img, (30, 30), (W - 30, H - 30), (60, 60, 60), 2)
    cv2.putText(img, "SLIDE %d" % (k + 1), (60, 110),
                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (20, 20, 20), 3)
    # 每页版面明显不同：色块位置 + 条形图 + 不同行数
    cv2.rectangle(img, (60, 150 + 20 * k), (300, 190 + 20 * k), (180, 120, 60), -1)
    for j in range(k + 1):
        cv2.rectangle(img, (340, 300 - 22 * j), (340 + 40 * (j + 1), 320 - 22 * j),
                      (60, 140, 200), -1)
    for j in range(5 - k):
        cv2.line(img, (60, 200 + 25 * j), (560, 200 + 25 * j), (200, 200, 200), 2)


def static(img, k):
    cv2.rectangle(img, (30, 30), (W - 30, H - 30), (60, 60, 60), 2)
    cv2.putText(img, "ONE SLIDE ONLY", (60, 140),
                cv2.FONT_HERSHEY_SIMPLEX, 1.3, (20, 20, 20), 3)
    for j in range(4):
        cv2.putText(img, "line %d" % (j + 1), (70, 200 + 26 * j),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 40), 2)


def subtle(img, k):
    cv2.rectangle(img, (30, 30), (W - 30, H - 30), (60, 60, 60), 2)
    cv2.putText(img, "PAGE %d" % (k + 1), (60, 100),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (20, 20, 20), 3)
    # 每页只多一行小字，版面几乎不变
    for j in range(k + 1):
        cv2.putText(img, "item %d" % (j + 1), (70, 180 + 24 * j),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "distinct"
    if mode == "distinct":
        make(mode, 60, 6, distinct, "test_distinct.mp4")
    elif mode == "static":
        make(mode, 30, 1, static, "test_static.mp4")
    elif mode == "subtle":
        make(mode, 60, 6, subtle, "test_subtle.mp4")
    else:
        raise SystemExit("unknown mode: " + mode)
