import React from 'react';
import {useCurrentFrame} from 'remotion';
import {FONT_CN, FONT_EN, clamp01, ease, paletteAt} from '../theme';

/**
 * 歌词动效：逐字浮起 → 落定 → 轻微呼吸 → 整句退场。
 *
 * 几个细节值得留意：
 *  - **逐字上浮**比整句淡入更有"被说出来"的感觉（每字错开 2~4 帧）
 *  - **每字相位不同**的呼吸让整句"活着"，而不是一块死字
 *  - **暗晕**（drop-shadow）是必须的：亮背景上白字会糊成一团
 *  - 中文按语义断句（rows），标点不落行首；中英混排分别处理字距
 */

export type LyricLineData = {
  idx: number;
  text: string;
  /** 语义断句后的每一行（权威来源：原歌词的换行） */
  rows: string[];
  /** 秒 */
  start: number;
  end: number;
};

type Props = {
  line: LyricLineData;
  /** 全片进度（用于取色） */
  t: number;
  fontSize?: number;
  /** 文字块垂直中心 */
  y?: number;
  /** 逐字间隔（帧） */
  stagger?: number;
  /** 覆盖时间轴（独立预览某一句时用） */
  inFrame?: number;
  outFrame?: number;
  /** 覆盖颜色（例如让字色跟着画面里的灯一起变冷变暖） */
  color?: string;
  glow?: string;
};

const FPS = 30;

export const LyricLine: React.FC<Props> = ({
  line, t, fontSize = 80, y = 812, stagger = 3, inFrame, outFrame, color, glow,
}) => {
  const f = useCurrentFrame();
  const p = paletteAt(t);
  const inkColor = color ?? p.ink;
  const glowColor = glow ?? p.warm;

  const F0 = inFrame ?? Math.round(line.start * FPS);
  const F1 = outFrame ?? Math.round(line.end * FPS);
  const age = f - F0;
  const exitP = clamp01((F1 - f) / 16);
  if (age < -12 || f > F1 + 6) return null;

  const lh = fontSize * 1.34;
  const blockH = line.rows.length * lh;

  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        top: y - blockH / 2,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        filter: 'drop-shadow(0 0 34px rgba(0,0,0,0.55))',
        pointerEvents: 'none',
      }}
    >
      {line.rows.map((row, ri) => {
        const isLatin = /^[\x00-\x7F\s'’]+$/.test(row);
        return (
          <div
            key={ri}
            style={{
              display: 'flex', justifyContent: 'center', alignItems: 'baseline',
              height: lh, whiteSpace: 'nowrap',
            }}
          >
            {[...row].map((ch, ci) => {
              if (ch === ' ') return <span key={ci} style={{width: fontSize * 0.34}} />;
              const d = ci * stagger + ri * 7;
              const pr = clamp01((age + 10 - d) / 20);
              const e = ease(pr);
              // 晚风般的呼吸：每字相位不同
              const breathe = Math.sin((f + ci * 6) * 0.045) * 2.6;
              return (
                <span
                  key={ci}
                  style={{
                    fontFamily: isLatin ? FONT_EN : FONT_CN,
                    fontStyle: isLatin ? 'italic' : 'normal',
                    fontSize: isLatin ? fontSize * 0.86 : fontSize,
                    letterSpacing: isLatin ? '0.06em' : '0.02em',
                    lineHeight: 1,
                    color: inkColor,
                    display: 'inline-block',
                    transform: `translateY(${(1 - e) * 38 + breathe + (1 - exitP) * -16}px)`,
                    opacity: e * exitP,
                    filter: `blur(${(1 - e) * 7}px)`,
                    textShadow: `0 0 46px ${glowColor}66, 0 3px 22px rgba(0,0,0,0.6)`,
                  }}
                >
                  {ch}
                </span>
              );
            })}
          </div>
        );
      })}
    </div>
  );
};
