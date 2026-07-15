import React from 'react';
import {AbsoluteFill, OffthreadVideo, useCurrentFrame, useVideoConfig, interpolate} from 'remotion';

// ── Teal "cutting-mat / blueprint" grid ─────────────────────────────────────
export const GridBG: React.FC<{color?: string}> = ({color = '#2f6f6a'}) => {
  const {width, height} = useVideoConfig();
  const step = 70;
  const lines: React.ReactNode[] = [];
  for (let x = 0; x <= width; x += step) {
    lines.push(<line key={`vx${x}`} x1={x} y1={0} x2={x} y2={height} stroke="rgba(255,255,255,0.12)" strokeWidth={x % (step * 2) === 0 ? 2 : 1} />);
  }
  for (let y = 0; y <= height; y += step) {
    lines.push(<line key={`hy${y}`} x1={0} y1={y} x2={width} y2={y} stroke="rgba(255,255,255,0.12)" strokeWidth={y % (step * 2) === 0 ? 2 : 1} />);
  }
  return (
    <AbsoluteFill style={{backgroundColor: color}}>
      <svg width="100%" height="100%">
        {lines}
        <line x1={0} y1={0} x2={width} y2={height} stroke="rgba(255,255,255,0.10)" strokeWidth={2} />
        <line x1={width} y1={0} x2={0} y2={height} stroke="rgba(255,255,255,0.10)" strokeWidth={2} />
      </svg>
    </AbsoluteFill>
  );
};

// ── Cardboard / kraft texture ───────────────────────────────────────────────
export const CardboardBG: React.FC = () => {
  const {width, height} = useVideoConfig();
  const flutes: React.ReactNode[] = [];
  for (let y = 0; y <= height; y += 26) {
    flutes.push(<line key={y} x1={0} y1={y} x2={width} y2={y} stroke="rgba(120,72,40,0.18)" strokeWidth={2} />);
  }
  return (
    <AbsoluteFill style={{background: 'linear-gradient(135deg,#b07a45,#8a5a30)'}}>
      <svg width="100%" height="100%">
        <filter id="cardgrain">
          <feTurbulence type="fractalNoise" baseFrequency="0.012 0.9" numOctaves="2" seed={4} />
          <feColorMatrix type="matrix" values="0 0 0 0 0.4  0 0 0 0 0.25  0 0 0 0 0.12  0 0 0 0.5 0" />
        </filter>
        <rect width="100%" height="100%" filter="url(#cardgrain)" opacity={0.5} />
        {flutes}
      </svg>
    </AbsoluteFill>
  );
};

// ── Solid bold colour ───────────────────────────────────────────────────────
export const SolidBG: React.FC<{color?: string}> = ({color = '#E7B10A'}) => (
  <AbsoluteFill style={{backgroundColor: color}} />
);

// ── Two-tone vertical split ─────────────────────────────────────────────────
export const SplitBG: React.FC<{color?: string; color2?: string}> = ({color = '#EDE6D6', color2 = '#E7B10A'}) => (
  <AbsoluteFill style={{background: `linear-gradient(90deg, ${color} 0 42%, ${color2} 42% 100%)`}} />
);

// ── AI B-roll shot (kept as an optional scene type) ─────────────────────────
export const BrollBG: React.FC<{src: string; zoom?: string; durationInFrames: number}> = ({src, zoom = 'none', durationInFrames}) => {
  const frame = useCurrentFrame();
  const from = zoom === 'out' ? 1.15 : 1.0;
  const to = zoom === 'out' ? 1.0 : zoom === 'in' ? 1.15 : 1.08;
  const scale = interpolate(frame, [0, durationInFrames], [from, to], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      <OffthreadVideo src={src} muted style={{width: '100%', height: '100%', objectFit: 'cover', transform: `scale(${scale})`}} />
    </AbsoluteFill>
  );
};

export type SceneBg = 'grid' | 'cardboard' | 'solid' | 'split' | 'broll';

export const SceneBackground: React.FC<{
  bg: SceneBg;
  color?: string;
  color2?: string;
  brollSrc?: string;
  zoom?: string;
  durationInFrames: number;
}> = ({bg, color, color2, brollSrc, zoom, durationInFrames}) => {
  switch (bg) {
    case 'grid':
      return <GridBG color={color} />;
    case 'cardboard':
      return <CardboardBG />;
    case 'solid':
      return <SolidBG color={color} />;
    case 'split':
      return <SplitBG color={color} color2={color2} />;
    case 'broll':
      return brollSrc ? <BrollBG src={brollSrc} zoom={zoom} durationInFrames={durationInFrames} /> : <SolidBG color={color} />;
    default:
      return <SolidBG color={color} />;
  }
};
