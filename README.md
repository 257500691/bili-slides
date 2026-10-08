# bili-slides

把 B 站课程视频转成**幻灯片 PDF**。

只做一件事：把视频里出现过的每一页幻灯片截出来，合成一份图片型 PDF。
**不转录、不做笔记、不做 OCR、不生成任何别的文件。**

适用场景：老师对着 PPT 讲课的录播课，而你只想要那份 PPT。

## 特性

- 输入可以是 **BV 号 / av 号 / 视频链接 / 合集分 P / 本地视频文件**
- `--all-parts` 把整个合集跑完并合成**一个 PDF**，按分 P 建书签
- **不需要 ffmpeg** —— 只下载画面流（单条流，无需混流），解码用 OpenCV 自带的 ffmpeg
- **自动屏蔽常动区域**：鼠标、摄像头小窗、循环动画
- **全局去重**：同一页在视频里反复出现（讲师来回翻页）、或同一页的批注/逐条展开中间状态，都会并成一页
- **过滤非幻灯片画面**：实拍、桌面录屏、手机拍纸
- **编码兜底**：B 站默认给 AV1/HEVC，解不开时自动改用 H.264 重下

## 安装

```bash
pip install yt-dlp opencv-python Pillow numpy pypdf
```

Python 3.9+，不需要安装 ffmpeg。

## 用法

```bash
# 单个视频（BV 号 / av 号 / 链接都行）
python bili_slides.py BV1gA4y1Z7GA

# 合集里的第 3 个分 P
python bili_slides.py "https://www.bilibili.com/video/BV1gA4y1Z7GA?p=3"

# 整个合集 -> 一个 PDF（带分 P 书签）
python bili_slides.py BV1gA4y1Z7GA --all-parts -o slides_collection

# 本地视频文件
python bili_slides.py --video lecture.mp4

# 1080P 及以上通常需要登录态
python bili_slides.py BV1gA4y1Z7GA --all-parts --cookies chrome
```

| 参数 | 说明 |
| --- | --- |
| `--all-parts` | 整个合集/多分 P 全跑，合成一个 PDF |
| `--cookies chrome\|edge\|firefox\|brave` | 借浏览器登录态下载高清晰度 |
| `--sensitivity high\|normal\|low` | 翻页灵敏度，默认 `normal` |
| `--dedupe-mae N` | 判为同一页的阈值，默认 `8.0`；调到 `2` 会把同页的批注/展开中间状态也各留一页 |
| `--keep-images` | 保留每页 PNG（默认只留 PDF） |
| `--keep-video` | 保留下载的视频文件（默认跑完删除） |

## 它是怎么做的

顺序解码整段视频，每秒比对 2 次画面，四步：

1. **校准** —— 先取一段连续窗口，统计每个 8×8 区块在相邻帧之间「变了多少次」。
   一直在动的区块（鼠标、摄像头小窗）整块排除在比较之外。
2. **扫描** —— 当前帧和**上一张被采纳的幻灯片**比较（不是和上一帧比），
   变化区块的比例超过阈值即判为翻页。
3. **导出** —— 在每一页停留区间的中段定位回源分辨率取帧，拿到的是这一页讲完时的状态，
   而不是翻页瞬间的过渡帧。
4. **去重与过滤** —— 全局比对 + 非幻灯片画面过滤。

### 两个实测出来的判据

**判据一：翻页阈值。** 同一页的噪声和真正的翻页，差了两个数量级：

| | 变化区块比例 |
| --- | --- |
| 同一页（含一直在动的摄像头小窗） | 0.0000 |
| 真正的翻页 | 0.095 ~ 0.138 |

**判据二：去重的方向性。** 去重不能只看像素差 —— 屏幕批注、逐条展开这类「同一页的另一个状态」，
像素差可以很大（实测 MAE 10.8），而真正的翻页最小只有 9.6，单靠阈值分不开。
能分开的是**变化的方向性**：

| | min(add, rem) / max(add, rem) |
| --- | --- |
| 同一页的批注/展开（单向：只加或只减） | **0.00** |
| 真正的翻页（旧内容消失 + 新内容出现） | **0.32 ~ 1.00** |

非幻灯片画面的过滤用了一个很干净的判据：真实数字幻灯片的「亮像素占比」在 50%~94%，
而混进来的实拍照片是 **0%**。

## 实测

一门 16 讲、总时长 15.4 小时的课程：

| 指标 | 结果 |
| --- | --- |
| 输入 | 15.4 小时视频（16 个分 P） |
| 输出 | **815 页**幻灯片，一个 PDF（34.7 MB，带 16 个分 P 书签） |
| 耗时 | **17.8 分钟**（约 52 倍速） |

## 已知限制

- 输出是**图片型 PDF**，不能选中/搜索文字。这是「只要幻灯片」的必然结果。
- 清晰度取决于片源：B 站未登录一般只有 480P，公式多的小字会糊。
- 如果课程是**逐条展开讲公式**，每一步都会成为一页，页数会明显多于 PPT 实际页数。
  想要保持每一步就用默认值；想合并成一页一图，把 `--dedupe-mae` 调大。
- 付费课程（哔哩哔哩课堂）需要已购账号的 cookie。

## 测试

`tests/gen_test.py` 生成合成「网课」视频用于自测：

```bash
python tests/gen_test.py distinct   # 6 页明显不同的幻灯片 + 一直在动的摄像头小窗
python tests/gen_test.py static     # 全程 1 页不变（考验会不会被噪声灌爆）
python tests/gen_test.py subtle     # 6 页只改一行小字（难点 case）
```

## 致谢

分块差分 + 常动区域屏蔽的思路来自
[video-slide-extractor](https://github.com/larry-xue/video-slide-extractor)（MIT）与
[learnopencv 的视频转幻灯片教程](https://learnopencv.com/video-to-slides-converter-using-background-subtraction/)。
本项目的实现、去重判据与合集流程是独立的。

## 版权

本工具只做格式转换。**视频与课件的版权归原作者**，请仅用于个人学习，不要二次分发他人的课程内容。

## License

MIT
