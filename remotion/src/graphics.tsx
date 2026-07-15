import React from 'react';
import {AbsoluteFill, OffthreadVideo, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';

// ── Hand-drawn arrow ring pointing inward at the subject ────────────────────
export const ArrowsRing: React.FC<{color?: string}> = ({color = '#ffffff'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const cx = width / 2;
  const cy = height / 2;
  const R = Math.min(width, height) * 0.46;
  const count = 12;
  const arrows: React.ReactNode[] = [];
  for (let i = 0; i < count; i++) {
    const ang = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x = cx + Math.cos(ang) * R;
    const y = cy + Math.sin(ang) * R;
    const appear = spring({frame, fps, config: {damping: 15}, delay: i * 1.2, durationInFrames: 8});
    const jitter = 3 * Math.sin((frame + i * 7) / 6);
    const deg = (ang * 180) / Math.PI + 180 + jitter; // point toward centre
    const s = 0.9 + 0.1 * Math.sin((frame + i * 9) / 8);
    arrows.push(
      <g key={i} transform={`translate(${x} ${y}) rotate(${deg}) scale(${appear * s})`} opacity={appear}>
        <path d="M -34 0 L 6 0 M -6 -14 L 10 0 L -6 14" fill="none" stroke={color} strokeWidth={7} strokeLinecap="round" strokeLinejoin="round" />
      </g>
    );
  }
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">{arrows}</svg>
    </AbsoluteFill>
  );
};

// ── Scribbly marker circle drawn around the subject ─────────────────────────
export const ScribbleCircle: React.FC<{color?: string}> = ({color = '#FFD400'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const cx = width / 2;
  const cy = height * 0.46;
  const rx = width * 0.4;
  const ry = height * 0.28;
  // Two offset scribble loops for a hand-drawn feel.
  const pts = (rOff: number, phase: number) => {
    let d = '';
    const steps = 60;
    for (let i = 0; i <= steps; i++) {
      const t = (i / steps) * Math.PI * 2;
      const wob = 1 + 0.04 * Math.sin(t * 6 + phase);
      const x = cx + Math.cos(t) * (rx + rOff) * wob;
      const y = cy + Math.sin(t) * (ry + rOff) * wob;
      d += i === 0 ? `M ${x} ${y}` : ` L ${x} ${y}`;
    }
    return d;
  };
  const draw = spring({frame, fps, config: {damping: 200}, durationInFrames: 18});
  const dash = 4200;
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <path d={pts(0, 0)} fill="none" stroke={color} strokeWidth={12} strokeLinecap="round"
          strokeDasharray={dash} strokeDashoffset={dash * (1 - draw)} />
        <path d={pts(18, 1.7)} fill="none" stroke={color} strokeWidth={8} strokeLinecap="round" opacity={0.85}
          strokeDasharray={dash} strokeDashoffset={dash * (1 - Math.max(0, draw - 0.15))} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Giant kinetic word(s) sitting BEHIND the presenter ──────────────────────
export const BigTextBehind: React.FC<{text: string; color?: string; shadow?: string}> = ({
  text,
  color = '#F0E6CE',
  shadow = 'rgba(0,0,0,0.35)',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 12, stiffness: 180, mass: 0.7}, durationInFrames: 12});
  const scale = interpolate(pop, [0, 1], [1.25, 1]);
  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
      <div
        style={{
          transform: `scale(${scale})`,
          fontFamily: '"Arial Black", Arial, sans-serif',
          fontWeight: 900,
          fontStyle: 'italic',
          fontSize: 250,
          lineHeight: 0.9,
          letterSpacing: -6,
          textAlign: 'center',
          textTransform: 'uppercase',
          color,
          textShadow: `10px 12px 0 ${shadow}`,
          padding: '0 20px',
          whiteSpace: 'pre-wrap',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// ── Presenter: full-bleed or "sticker" cutout with outline + shadow ─────────
export const Presenter: React.FC<{src: string; mode: 'full' | 'sticker'; punch?: boolean}> = ({src, mode, punch}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, mass: 0.7}, durationInFrames: 12});
  const punchScale = punch ? interpolate(frame, [0, 8], [1.0, 1.18], {extrapolateRight: 'clamp'}) : 1;
  const breathe = 1 + 0.008 * Math.sin(frame / 24);

  if (mode === 'sticker') {
    // white sticker outline via stacked drop-shadows + a soft cast shadow
    const outline = '#ffffff';
    const shadows = [
      ...Array.from({length: 16}).map((_, i) => {
        const a = (i / 16) * Math.PI * 2;
        return `drop-shadow(${Math.cos(a) * 6}px ${Math.sin(a) * 6}px 0 ${outline})`;
      }),
      'drop-shadow(0 24px 26px rgba(0,0,0,0.45))',
    ].join(' ');
    const y = interpolate(enter, [0, 1], [70, 0]);
    return (
      <AbsoluteFill style={{transform: `translateY(${y}px) scale(${breathe * punchScale})`, filter: shadows}}>
        <OffthreadVideo src={src} transparent style={{width: '100%', height: '100%', objectFit: 'contain', objectPosition: 'bottom center'}} />
      </AbsoluteFill>
    );
  }
  const y = interpolate(enter, [0, 1], [40, 0]);
  return (
    <AbsoluteFill style={{transform: `translateY(${y}px) scale(${breathe * punchScale})`}}>
      <OffthreadVideo src={src} transparent style={{width: '100%', height: '100%', objectFit: 'cover'}} />
    </AbsoluteFill>
  );
};

// ── Circular lens vignette (scope look) ─────────────────────────────────────
export const LensVignette: React.FC = () => (
  <AbsoluteFill style={{pointerEvents: 'none'}}>
    <AbsoluteFill style={{boxShadow: 'inset 0 0 220px 140px rgba(0,0,0,0.9)', borderRadius: '50%'}} />
    <AbsoluteFill style={{boxShadow: 'inset 0 0 120px 40px rgba(0,0,0,0.55)'}} />
  </AbsoluteFill>
);

// ── Word-by-word kinetic captions ───────────────────────────────────────────
export type Word = {start: number; end: number; text: string};

export const WordCaptions: React.FC<{words: Word[]; position?: 'top' | 'bottom'; highlight?: string}> = ({
  words,
  position = 'bottom',
  highlight = '#FFE600',
}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const t = frame / fps;
  // group into chunks of up to 3 words for readability
  const CHUNK = 3;
  const chunks: Word[][] = [];
  for (let i = 0; i < words.length; i += CHUNK) chunks.push(words.slice(i, i + CHUNK));
  const active = chunks.find((c) => t >= c[0].start && t < c[c.length - 1].end);
  if (!active) return null;
  const chunkStartFrame = Math.round(active[0].start * fps);
  const local = frame - chunkStartFrame;
  const pop = spring({frame: local, fps, config: {damping: 13, stiffness: 220, mass: 0.5}, durationInFrames: 8});
  const scale = interpolate(pop, [0, 1], [0.72, 1]);
  return (
    <AbsoluteFill
      style={{
        justifyContent: position === 'top' ? 'flex-start' : 'flex-end',
        alignItems: 'center',
        paddingTop: position === 'top' ? 220 : 0,
        paddingBottom: position === 'bottom' ? 340 : 0,
        paddingLeft: 60,
        paddingRight: 60,
      }}
    >
      <div style={{transform: `scale(${scale})`, textAlign: 'center', display: 'flex', flexWrap: 'wrap', gap: 16, justifyContent: 'center'}}>
        {active.map((w, i) => {
          const on = t >= w.start;
          return (
            <span
              key={i}
              style={{
                fontFamily: '"Arial Black", Arial, sans-serif',
                fontWeight: 900,
                fontSize: 78,
                lineHeight: 1.02,
                textTransform: 'uppercase',
                color: on ? highlight : '#ffffff',
                WebkitTextStroke: '4px #000',
                paintOrder: 'stroke fill',
                textShadow: '0 6px 16px rgba(0,0,0,0.85)',
              }}
            >
              {w.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
