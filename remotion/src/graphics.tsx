import React from 'react';
import {AbsoluteFill, OffthreadVideo, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';

// A wobbly hand-drawn arrow: tapered double-stroke shaft + chunky head, with a
// slight roughness so it reads as marker, not vector.
const MarkerArrow: React.FC<{color: string; jitter: number}> = ({color, jitter}) => {
  const w = 9;
  return (
    <g strokeLinecap="round" strokeLinejoin="round" fill="none">
      {/* soft dark backing for contrast on busy video */}
      <path d={`M -46 ${jitter} C -26 ${jitter - 5}, -10 ${jitter + 4}, 8 0`} stroke="rgba(0,0,0,0.35)" strokeWidth={w + 8} />
      <path d="M -8 -17 L 12 0 L -8 17" stroke="rgba(0,0,0,0.35)" strokeWidth={w + 8} />
      {/* marker stroke */}
      <path d={`M -46 ${jitter} C -26 ${jitter - 5}, -10 ${jitter + 4}, 8 0`} stroke={color} strokeWidth={w} />
      <path d="M -8 -17 L 12 0 L -8 17" stroke={color} strokeWidth={w} />
    </g>
  );
};

// ── Hand-drawn arrow ring pointing inward at the subject ────────────────────
export const ArrowsRing: React.FC<{color?: string}> = ({color = '#ffffff'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const cx = width / 2;
  const cy = height / 2;
  const R = Math.min(width, height) * 0.47;
  const count = 10;
  const arrows: React.ReactNode[] = [];
  for (let i = 0; i < count; i++) {
    const ang = (i / count) * Math.PI * 2 - Math.PI / 2;
    const breathe = 1 + 0.04 * Math.sin((frame + i * 6) / 7);
    const x = cx + Math.cos(ang) * R * breathe;
    const y = cy + Math.sin(ang) * R * breathe;
    const appear = spring({frame, fps, config: {damping: 13, mass: 0.5}, delay: i * 1.4, durationInFrames: 9});
    const jitter = 4 * Math.sin((frame + i * 11) / 5);
    const deg = (ang * 180) / Math.PI + 180 + jitter;
    const s = (0.95 + 0.08 * Math.sin((frame + i * 9) / 8)) * appear;
    arrows.push(
      <g key={i} transform={`translate(${x} ${y}) rotate(${deg}) scale(${s})`} opacity={appear}>
        <MarkerArrow color={color} jitter={jitter} />
      </g>
    );
  }
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">{arrows}</svg>
    </AbsoluteFill>
  );
};

// ── Scribbly marker circle drawn around the subject (rough, multi-loop) ──────
export const ScribbleCircle: React.FC<{color?: string}> = ({color = '#FFD400'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const cx = width / 2;
  const cy = height * 0.45;
  const rx = width * 0.42;
  const ry = height * 0.3;
  // Three overlapping loops, each with high-frequency wobble for a marker scrawl.
  const pts = (rOff: number, phase: number, wob: number) => {
    let d = '';
    const steps = 90;
    for (let i = 0; i <= steps; i++) {
      const t = (i / steps) * Math.PI * 2 * 1.08; // overshoot so ends cross
      const r = 1 + wob * Math.sin(t * 7 + phase) + 0.02 * Math.sin(t * 19 + phase);
      const x = cx + Math.cos(t + 0.15) * (rx + rOff) * r;
      const y = cy + Math.sin(t) * (ry + rOff) * r;
      d += i === 0 ? `M ${x} ${y}` : ` L ${x} ${y}`;
    }
    return d;
  };
  const draw = spring({frame, fps, config: {damping: 200}, durationInFrames: 20});
  const dash = 6000;
  const loop = (d: string, sw: number, op: number, delay: number) => (
    <path d={d} fill="none" stroke={color} strokeWidth={sw} strokeLinecap="round" opacity={op}
      strokeDasharray={dash} strokeDashoffset={dash * (1 - Math.max(0, draw - delay))} />
  );
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <g style={{filter: 'drop-shadow(0 4px 6px rgba(0,0,0,0.35))'}}>
          {loop(pts(0, 0, 0.05), 16, 1, 0)}
          {loop(pts(22, 2.1, 0.06), 11, 0.9, 0.12)}
          {loop(pts(-16, 4.0, 0.045), 8, 0.75, 0.22)}
        </g>
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
