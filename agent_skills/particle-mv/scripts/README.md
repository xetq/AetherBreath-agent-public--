# scripts/ —— 音频对齐工具链

把「一首歌」变成「一份权威时间轴」的五个命令行工具。
它们把人工判断压到最少：**人只负责听和判断，机器负责测量和计算。**

```
歌 ──[Demucs 分离]──► vocals.wav
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
  analyze_audio.py   make_chart.py     refine_anchors.py
  （能量包络→候选分段）  （对照图给人看）    （粗锚点→毫秒精修）
        │                 │                  │
        └─────────────────┴──────────────────┘
                          ▼
                   build_timeline.py
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        timeline.json              lyrics.ts
              │
              ▼
      make_slices.py  ──►  slices/line_01.wav …（交给人类试听核对）
```

## 依赖

```bash
pip install numpy            # analyze / refine / make_chart / build_timeline
pip install pillow           # 仅 make_chart（画图）
# ffmpeg 需要在 PATH 里，或者用 --ffmpeg 指定路径（仅 make_slices）
```

## 各脚本

### `analyze_audio.py` —— 先摸清这首歌的结构

```bash
python analyze_audio.py vocals.wav --out data/env.npy
```

打印三档粒度的分段：`0.25s` 句内停顿 / `0.55s` 句子边界 / `1.10s` 段落边界。
**先用大的切段落、再用小的切句子**，一次拿到两层结构。

### `make_chart.py` —— 出一张给人看的对照图

```bash
python make_chart.py vocals.wav chart.png --duration 100 \
    --cuts refined.json --marks "83.59:人声段结束,93.0:成片结束"
```

波形 + 能量包络 + 切点竖线 + 秒数刻度，全在同一张图上。
让人一眼看出"哪条线歪了"——比在播放器里来回拖快得多。

### `refine_anchors.py` —— 粗锚点精修成毫秒（★ 最有用的一个）

```bash
python refine_anchors.py vocals.wav anchors.txt --out refined.json
```

**这是"人在回路"最省力的形态**：
人在 `anchors.txt` 里写"这句大概在 15 秒"（听感给出的近似值就够了），
算法在前后 1.2 秒的窗口里找**能量爬升最陡的那一点**——那才是真正的起唱。

```text
# anchors.txt
1,这是一封离别信,15.0
2,写下我该离开的原因,18.0
```

它还会做两项体检：**单调性**（时间必须递增）和**间隔检查**（相邻句间隔过短 = 可能有重复锚点）。
位移超过 0.6 秒的行会被标出来提醒复听。

### `make_slices.py` —— 切片给人试听

```bash
python make_slices.py song.wav timeline.json slices/ --pad 0.8
```

每句切一个 wav，**前后各留 0.8 秒缓冲**。缓冲不是可选项——
人需要听到"这句怎么收、下一句怎么进"才判断得出边界对不对。

### `build_timeline.py` —— 定稿成数据

```bash
python build_timeline.py anchors.txt out/ --mv-end 93 --fps 30 --intro-end 15.49 --outro-start 83.59
```

产出 `timeline.json`（含帧号）和 `lyrics.ts`（直接丢进前端工程）。
生成后记得**按原歌词的换行**把 `rows` 手工调一遍——那是语义断句的权威来源。

---

## 完整走一遍

```bash
# 0) 分离人声（国内加镜像，否则拉模型时会静默空等）
HF_ENDPOINT=https://hf-mirror.com python -m demucs -n htdemucs -o separated song.wav

# 1) 先看结构
python scripts/analyze_audio.py separated/htdemucs/vocals.wav --out data/env.npy

# 2) 拿候选边界（从上面的输出里抄，或让脚本导出）
#    如果作者能提供粗略锚点，直接写 anchors.txt 跳到第 3 步

# 3) 精修 + 出图
python scripts/refine_anchors.py separated/htdemucs/vocals.wav anchors.txt --out refined.json
python scripts/make_chart.py separated/htdemucs/vocals.wav chart.png --cuts refined.json

# 4) 定稿
python scripts/build_timeline.py anchors.txt out/ --mv-end 93 --fps 30

# 5) 切片，交给人类听（这一步不能省）
python scripts/make_slices.py song.wav out/timeline.json slices/ --pad 0.8
```

**第 5 步之后**：让对方只听不测，回答"第 N 句早了/晚了"。
通常只有 2~4 句要改，其余自动结果直接可用。

---

## 三个不要

1. **不要用混音做能量分析** —— 鼓和贝斯会让"换气"看起来像"还在唱"，先分离人声
2. **不要用绝对阈值** —— 不同歌的录音电平差很多，用「相对响度 90 分位往下压 22 dB」
3. **不要跳过第 5 步** —— 气口、长音、和声、切分抢拍、第一句，这五类位置算法一定会出错
