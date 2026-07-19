import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';

// Shared helpers ------------------------------------------------------------
const useDraw = (dur = 12, delay = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame, fps, config: {damping: 200}, durationInFrames: dur, delay});
};
const usePop = (dur = 10, delay = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame, fps, config: {damping: 12, stiffness: 200, mass: 0.6}, durationInFrames: dur, delay});
};
const shadow = 'drop-shadow(0 4px 6px rgba(0,0,0,0.45))';

// ── Single bold arrow pointing UP at the subject from the lower-left ─────────
// Sits low and to the side so it never covers the face (upper-centre) or the
// centered captions.
export const BigArrow: React.FC<{color?: string}> = ({color = '#FFE600'}) => {
  const {width, height} = useVideoConfig();
  const draw = useDraw(12);
  const frame = useCurrentFrame();
  const wob = 3 * Math.sin(frame / 6);
  // shaft curves up from the bottom-left toward the subject's body/shoulder
  const x0 = width * 0.14, y0 = height * 0.82;
  const x1 = width * 0.30, y1 = height * 0.56;
  const d = `M ${x0} ${y0} C ${x0 - 30} ${y0 - 140}, ${x1 - 70} ${y1 + 150}, ${x1 + wob} ${y1}`;
  const dash = 1400;
  const head = usePop(9, 8);
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <g style={{filter: shadow}} fill="none" stroke={color} strokeLinecap="round" strokeLinejoin="round">
          <path d={d} strokeWidth={22} strokeDasharray={dash} strokeDashoffset={dash * (1 - draw)} />
          <g transform={`translate(${x1 + wob} ${y1}) rotate(-108) scale(${head})`}>
            <path d="M -46 -34 L 0 0 L -46 34" strokeWidth={22} />
          </g>
        </g>
      </svg>
    </AbsoluteFill>
  );
};

// ── Reworked arrow ring — fewer, bolder, all draw-on toward centre ──────────
export const ArrowsRing: React.FC<{color?: string}> = ({color = '#ffffff'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const cx = width / 2, cy = height / 2;
  const R = Math.min(width, height) * 0.48;
  const count = 6;
  const items: React.ReactNode[] = [];
  for (let i = 0; i < count; i++) {
    const ang = (i / count) * Math.PI * 2 - Math.PI / 2;
    const breathe = 1 + 0.03 * Math.sin((frame + i * 8) / 8);
    const x = cx + Math.cos(ang) * R * breathe;
    const y = cy + Math.sin(ang) * R * breathe;
    const pop = spring({frame, fps, config: {damping: 13, mass: 0.5}, delay: i * 2, durationInFrames: 9});
    const deg = (ang * 180) / Math.PI + 180;
    items.push(
      <g key={i} transform={`translate(${x} ${y}) rotate(${deg}) scale(${pop})`} opacity={pop}
        style={{filter: shadow}} fill="none" stroke={color} strokeLinecap="round" strokeLinejoin="round">
        <path d="M -54 0 L 4 0" strokeWidth={13} />
        <path d="M -14 -22 L 12 0 L -14 22" strokeWidth={13} />
      </g>
    );
  }
  return <AbsoluteFill style={{pointerEvents: 'none'}}><svg width="100%" height="100%">{items}</svg></AbsoluteFill>;
};

// ── Clean hand-drawn circle highlight around the presenter's face/upper body ─
export const CircleHighlight: React.FC<{color?: string}> = ({color = '#FF3B3B'}) => {
  const {width, height} = useVideoConfig();
  const draw = useDraw(16);
  // Full-frame talking head → face sits in the upper-centre. Frame head+shoulders.
  const cx = width / 2, cy = height * 0.34;
  const rx = width * 0.34, ry = height * 0.23;
  let d = '';
  const steps = 80;
  for (let i = 0; i <= steps; i++) {
    const t = -Math.PI / 2 + (i / steps) * Math.PI * 2 * 1.06; // slight overshoot
    const wob = 1 + 0.02 * Math.sin(t * 5);
    const x = cx + Math.cos(t) * rx * wob + 8 * Math.sin(t * 3);
    const y = cy + Math.sin(t) * ry * wob;
    d += i === 0 ? `M ${x} ${y}` : ` L ${x} ${y}`;
  }
  const dash = 5200;
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <path d={d} fill="none" stroke={color} strokeWidth={14} strokeLinecap="round"
          style={{filter: shadow}} strokeDasharray={dash} strokeDashoffset={dash * (1 - draw)} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Underline swoosh under the caption ──────────────────────────────────────
export const Underline: React.FC<{color?: string; position?: 'top' | 'bottom'}> = ({color = '#FFE600', position = 'bottom'}) => {
  const {width, height} = useVideoConfig();
  const draw = useDraw(11);
  const y = position === 'top' ? height * 0.24 : height * 0.8;
  const d = `M ${width * 0.2} ${y} C ${width * 0.42} ${y + 20}, ${width * 0.62} ${y - 14}, ${width * 0.82} ${y - 4}`;
  const dash = 1000;
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <path d={d} fill="none" stroke={color} strokeWidth={15} strokeLinecap="round"
          style={{filter: shadow}} strokeDasharray={dash} strokeDashoffset={dash * (1 - draw)} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Highlighter swipe behind the caption band ───────────────────────────────
export const Highlighter: React.FC<{color?: string; position?: 'top' | 'bottom'}> = ({color = '#FFE600', position = 'bottom'}) => {
  const draw = useDraw(9);
  const {height} = useVideoConfig();
  const top = position === 'top' ? '20%' : '76%';
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <div style={{position: 'absolute', top, left: '12%', height: height * 0.07,
        width: `${76 * draw}%`, background: color, opacity: 0.55, mixBlendMode: 'multiply',
        borderRadius: 8, transform: 'rotate(-1.5deg)'}} />
    </AbsoluteFill>
  );
};

// ── Hand-drawn rounded box around the subject ───────────────────────────────
export const HandBox: React.FC<{color?: string}> = ({color = '#ffffff'}) => {
  const {width, height} = useVideoConfig();
  const draw = useDraw(16);
  const x = width * 0.13, y = height * 0.18, w = width * 0.74, h = height * 0.62, r = 40;
  const d = `M ${x + r} ${y} L ${x + w - r} ${y} Q ${x + w} ${y} ${x + w} ${y + r}
    L ${x + w} ${y + h - r} Q ${x + w} ${y + h} ${x + w - r} ${y + h}
    L ${x + r} ${y + h} Q ${x} ${y + h} ${x} ${y + h - r}
    L ${x} ${y + r} Q ${x} ${y} ${x + r} ${y}`;
  const dash = 7000;
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <path d={d} fill="none" stroke={color} strokeWidth={12} strokeLinecap="round"
          style={{filter: shadow}} strokeDasharray={dash} strokeDashoffset={dash * (1 - draw)} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Camera focus brackets framing the presenter's face/upper body ───────────
export const CornerBrackets: React.FC<{color?: string}> = ({color = '#FFE600'}) => {
  const {width, height} = useVideoConfig();
  const s = usePop(12);
  const len = 130 * s, t = 12;
  // A box around the upper-centre subject rather than the whole 9:16 frame.
  const x0 = width * 0.16, y0 = height * 0.12, x1 = width * 0.84, y1 = height * 0.6;
  const B = (x: number, y: number, k: string) => (
    <g key={k} style={{filter: shadow}}>
      <rect x={x} y={y} width={t} height={len} fill={color} />
      <rect x={x} y={y} width={len} height={t} fill={color} />
    </g>
  );
  return (
    <AbsoluteFill style={{pointerEvents: 'none', opacity: s}}>
      <svg width="100%" height="100%">
        {B(x0, y0, 'tl')}
        <g transform={`translate(${x1} ${y0}) scale(-1 1)`}>{B(0, 0, 'tr')}</g>
        <g transform={`translate(${x0} ${y1}) scale(1 -1)`}>{B(0, 0, 'bl')}</g>
        <g transform={`translate(${x1} ${y1}) scale(-1 -1)`}>{B(0, 0, 'br')}</g>
      </svg>
    </AbsoluteFill>
  );
};

// ── Sparkle / star pops ─────────────────────────────────────────────────────
export const StarPops: React.FC<{color?: string}> = ({color = '#FFE600'}) => {
  const {width, height, fps} = useVideoConfig();
  const frame = useCurrentFrame();
  const seeds = [[0.24, 0.28], [0.78, 0.32], [0.7, 0.7], [0.2, 0.66], [0.5, 0.2]];
  const star = 'M0,-30 L8,-8 L30,0 L8,8 L0,30 L-8,8 L-30,0 L-8,-8 Z';
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        {seeds.map(([px, py], i) => {
          const s = spring({frame, fps, config: {damping: 12, mass: 0.6}, delay: i * 4, durationInFrames: 10});
          const rot = (frame + i * 20) * 2;
          return (
            <g key={i} transform={`translate(${px * width} ${py * height}) rotate(${rot}) scale(${s})`}
              style={{filter: shadow}} fill={color} opacity={s}>
              <path d={star} />
            </g>
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

// ── Manga action / speed lines radiating from centre (hype) ─────────────────
export const ActionLines: React.FC<{color?: string}> = ({color = '#ffffff'}) => {
  const {width, height} = useVideoConfig();
  const s = usePop(8);
  const cx = width / 2, cy = height / 2;
  const N = 28;
  const lines: React.ReactNode[] = [];
  for (let i = 0; i < N; i++) {
    const a = (i / N) * Math.PI * 2;
    const inner = Math.min(width, height) * 0.42;
    const outer = Math.max(width, height) * 0.9;
    lines.push(
      <line key={i} x1={cx + Math.cos(a) * inner} y1={cy + Math.sin(a) * inner}
        x2={cx + Math.cos(a) * outer} y2={cy + Math.sin(a) * outer}
        stroke={color} strokeWidth={i % 2 ? 6 : 10} strokeLinecap="round" opacity={0.5 * s} />
    );
  }
  return <AbsoluteFill style={{pointerEvents: 'none'}}><svg width="100%" height="100%">{lines}</svg></AbsoluteFill>;
};

// ── Big hand-drawn check ✓ or cross ✗ stamp ─────────────────────────────────
export const Mark: React.FC<{kind: 'check' | 'cross'; color?: string}> = ({kind, color}) => {
  const {width, height} = useVideoConfig();
  const s = usePop(10);
  const c = color || (kind === 'check' ? '#33E06A' : '#FF3B3B');
  const cx = width * 0.5, cy = height * 0.3;
  const draw = useDraw(10);
  const dash = 700;
  const paths = kind === 'check'
    ? [`M ${cx - 70} ${cy} L ${cx - 15} ${cy + 60} L ${cx + 80} ${cy - 70}`]
    : [`M ${cx - 65} ${cy - 65} L ${cx + 65} ${cy + 65}`, `M ${cx + 65} ${cy - 65} L ${cx - 65} ${cy + 65}`];
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <g transform={`scale(${0.6 + 0.4 * s})`} style={{transformOrigin: `${cx}px ${cy}px`, filter: shadow}}
          fill="none" stroke={c} strokeWidth={26} strokeLinecap="round" strokeLinejoin="round">
          {paths.map((d, i) => (
            <path key={i} d={d} strokeDasharray={dash} strokeDashoffset={dash * (1 - Math.max(0, draw - i * 0.2))} />
          ))}
        </g>
      </svg>
    </AbsoluteFill>
  );
};

// ── Emoji pop — small, in a corner safe-zone (never over the face) ──────────
// slot picks a corner so it never lands on the centre subject or an infographic.
export const EmojiPop: React.FC<{emoji: string; slot?: 'tl' | 'tr' | 'bl' | 'br'}> = ({emoji, slot = 'tr'}) => {
  const s = usePop(11);
  const frame = useCurrentFrame();
  const wob = 5 * Math.sin(frame / 6);
  const pos: React.CSSProperties =
    slot === 'tl' ? {top: '14%', left: '9%'} :
    slot === 'tr' ? {top: '14%', right: '9%'} :
    slot === 'bl' ? {bottom: '28%', left: '9%'} :
                    {bottom: '28%', right: '9%'};
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <div style={{position: 'absolute', ...pos, fontSize: 150,
        transform: `scale(${s}) rotate(${wob}deg)`, filter: 'drop-shadow(0 8px 14px rgba(0,0,0,0.5))'}}>{emoji}</div>
    </AbsoluteFill>
  );
};

export const Doodle: React.FC<{kind: string; captionPos?: 'top' | 'bottom'}> = ({kind, captionPos = 'bottom'}) => {
  switch (kind) {
    case 'arrow': return <BigArrow />;
    case 'arrows': return <ArrowsRing />;
    case 'circle': return <CircleHighlight />;
    case 'underline': return <Underline position={captionPos} />;
    case 'highlighter': return <Highlighter position={captionPos} />;
    case 'box': return <HandBox />;
    case 'brackets': return <CornerBrackets />;
    case 'stars': return <StarPops />;
    case 'action_lines': return <ActionLines />;
    case 'check': return <Mark kind="check" />;
    case 'cross': return <Mark kind="cross" />;
    default: return null;
  }
};
