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
// Static cover — the per-scene zoom is applied in the FRONT pass to the whole
// composited shot (background + presenter together), so the back plate must not
// zoom or it would double-zoom and mismatch the keyed presenter.
export const BrollBG: React.FC<{src: string; zoom?: string; durationInFrames: number}> = ({src}) => {
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      <OffthreadVideo src={src} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
    </AbsoluteFill>
  );
};

// ── Retro camcorder / media-player UI frame (drawn OVER the scene) ──────────
export const RecUIFrame: React.FC = () => {
  const frame = useCurrentFrame();
  const {width, height, fps} = useVideoConfig();
  const blink = Math.floor(frame / (fps / 2)) % 2 === 0;
  const secs = Math.floor(frame / fps);
  const tc = `00:${String(secs).padStart(2, '0')}:${String(frame % fps).padStart(2, '0')}`;
  const m = 46;
  const corner = (x: number, y: number, sx: number, sy: number) => (
    <g transform={`translate(${x} ${y}) scale(${sx} ${sy})`}>
      <path d="M0,64 L0,0 L64,0" fill="none" stroke="#fff" strokeWidth={7} opacity={0.92} />
    </g>
  );
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        {corner(m, m, 1, 1)}
        {corner(width - m, m, -1, 1)}
        {corner(m, height - m, 1, -1)}
        {corner(width - m, height - m, -1, -1)}
      </svg>
      {/* REC dot + timecode */}
      <div style={{position: 'absolute', top: m + 22, left: m + 34, display: 'flex', alignItems: 'center', gap: 14}}>
        <div style={{width: 26, height: 26, borderRadius: '50%', background: blink ? '#FF3B30' : 'transparent',
          boxShadow: blink ? '0 0 18px rgba(255,59,48,0.9)' : 'none'}} />
        <span style={{fontFamily: 'monospace', fontWeight: 700, fontSize: 38, color: '#fff',
          textShadow: '0 2px 6px rgba(0,0,0,0.8)'}}>REC</span>
      </div>
      <div style={{position: 'absolute', top: m + 26, right: m + 34, fontFamily: 'monospace',
        fontSize: 34, color: '#fff', textShadow: '0 2px 6px rgba(0,0,0,0.8)'}}>{tc}</div>
      {/* battery */}
      <div style={{position: 'absolute', bottom: m + 30, right: m + 34, display: 'flex', alignItems: 'center', gap: 6}}>
        <div style={{width: 62, height: 30, border: '4px solid #fff', borderRadius: 5, padding: 3}}>
          <div style={{width: '62%', height: '100%', background: '#fff'}} />
        </div>
        <div style={{width: 7, height: 14, background: '#fff', borderRadius: 2}} />
      </div>
      {/* playback bar */}
      <div style={{position: 'absolute', bottom: m + 34, left: m + 34, right: m + 150, height: 10,
        background: 'rgba(255,255,255,0.35)', borderRadius: 5}}>
        <div style={{width: `${Math.min(100, (frame / Math.max(1, fps * 3)) * 100)}%`, height: '100%',
          background: '#fff', borderRadius: 5}} />
      </div>
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
