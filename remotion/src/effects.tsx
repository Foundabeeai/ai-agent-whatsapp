import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';

// ── Per-cut film "filters": each B-roll shot gets a different graded look ────
export const FILM_FILTERS = [
  'saturate(1.15) contrast(1.08)',
  'sepia(0.30) saturate(1.25) contrast(1.06) brightness(1.02)', // warm super-8
  'hue-rotate(-10deg) saturate(1.35) contrast(1.12)',           // teal / orange
  'grayscale(0.18) contrast(1.18) brightness(1.03)',            // moody film
  'saturate(1.45) contrast(1.10) brightness(1.02)',             // punchy vibrant
  'sepia(0.15) hue-rotate(8deg) saturate(1.2) contrast(1.1)',   // vintage cool
];

export const filterFor = (i: number) => FILM_FILTERS[i % FILM_FILTERS.length];

// ── Film grain (animated noise, overlay blend) ──────────────────────────────
export const FilmGrain: React.FC<{opacity?: number}> = ({opacity = 0.1}) => {
  const frame = useCurrentFrame();
  const seed = frame % 12; // cycle a few noise fields so grain shimmers cheaply
  return (
    <AbsoluteFill style={{opacity, mixBlendMode: 'overlay', pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <filter id={`grain${seed}`}>
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed={seed} stitchTiles="stitch" />
        </filter>
        <rect width="100%" height="100%" filter={`url(#grain${seed})`} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Vignette to focus the eye centre-frame ──────────────────────────────────
export const Vignette: React.FC = () => (
  <AbsoluteFill style={{boxShadow: 'inset 0 0 320px 130px rgba(0,0,0,0.55)', pointerEvents: 'none'}} />
);

// ── Drifting warm light leaks (screen blend) ────────────────────────────────
const OneLeak: React.FC<{hue: number; seed: number; strength: number}> = ({hue, seed, strength}) => {
  const frame = useCurrentFrame();
  const x = 30 + 45 * Math.sin((frame + seed) / 42);
  const y = 25 + 35 * Math.cos((frame + seed) / 57);
  const op = strength * (0.55 + 0.45 * Math.sin((frame + seed) / 26));
  return (
    <AbsoluteFill
      style={{
        mixBlendMode: 'screen',
        pointerEvents: 'none',
        background: `radial-gradient(circle at ${x}% ${y}%, hsla(${hue}, 90%, 62%, ${op}), transparent 46%)`,
      }}
    />
  );
};

export const LightLeaks: React.FC = () => (
  <>
    <OneLeak hue={32} seed={0} strength={0.35} />
    <OneLeak hue={280} seed={120} strength={0.22} />
  </>
);

// ── Clean cut transition: a light sweep + brief flash across the cut ─────────
// Placed in a short Sequence centred on each cut boundary so the hard cut reads
// as an intentional, polished transition.
export const CutFlash: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const op = interpolate(frame, [0, durationInFrames * 0.35, durationInFrames], [0, 0.55, 0], {
    extrapolateRight: 'clamp',
  });
  const pos = interpolate(frame, [0, durationInFrames], [-25, 125]);
  return (
    <AbsoluteFill
      style={{
        mixBlendMode: 'screen',
        opacity: op,
        pointerEvents: 'none',
        background: `linear-gradient(100deg, transparent ${pos - 28}%, rgba(255,240,210,0.95) ${pos}%, transparent ${pos + 28}%)`,
      }}
    />
  );
};

// ── Shape accents: animated corner brackets framing an emphasis shot ─────────
export const ShapeAccent: React.FC<{color?: string}> = ({color = '#FFE600'}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const s = spring({frame, fps, durationInFrames: 12, config: {damping: 14, mass: 0.6}});
  const len = interpolate(s, [0, 1], [0, 180]);
  const inset = 70;
  const t = 8; // stroke thickness
  const corner = (x: number, y: number, dx: number, dy: number, key: string) => (
    <g key={key}>
      <rect x={x} y={y} width={dx > 0 ? len : t} height={dx > 0 ? t : len} fill={color} />
      <rect x={x} y={y} width={dy > 0 ? t : len} height={dy > 0 ? len : t} fill={color} />
    </g>
  );
  return (
    <AbsoluteFill style={{opacity: 0.85 * s, pointerEvents: 'none'}}>
      <svg width="100%" height="100%" viewBox={`0 0 ${width} ${height}`}>
        {corner(inset, inset, 1, 1, 'tl')}
        {corner(width - inset - len, inset, 1, 1, 'tr')}
        {corner(inset, height - inset - len, 1, 1, 'bl')}
        {corner(width - inset - len, height - inset - len, 1, 1, 'br')}
      </svg>
    </AbsoluteFill>
  );
};
