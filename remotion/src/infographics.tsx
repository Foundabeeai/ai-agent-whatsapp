import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';

const fmt = (n: number) => {
  if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n % 1000 ? 1 : 0) + 'K';
  return String(Math.round(n));
};

const bold900: React.CSSProperties = {fontFamily: '"Arial Black", Arial, sans-serif', fontWeight: 900};
const cardShadow = '0 18px 40px rgba(0,0,0,0.45)';

// ── Number counter ticking up to `value` ────────────────────────────────────
export const Counter: React.FC<{value: number; label?: string; suffix?: string}> = ({value, label, suffix}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps * 1.1)});
  const n = Math.round(value * p);
  const pop = spring({frame, fps, config: {damping: 12}, durationInFrames: 10});
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '16%', transform: `scale(${interpolate(pop, [0, 1], [0.7, 1])})`, textAlign: 'center'}}>
        <div style={{...bold900, fontSize: 190, color: '#fff', WebkitTextStroke: '6px #000', paintOrder: 'stroke fill',
          textShadow: '0 8px 20px rgba(0,0,0,0.6)', lineHeight: 1}}>
          {fmt(n)}{suffix || ''}
        </div>
        {label ? <div style={{...bold900, fontSize: 52, color: '#FFE600', textTransform: 'uppercase',
          WebkitTextStroke: '3px #000', paintOrder: 'stroke fill', marginTop: 8}}>{label}</div> : null}
      </div>
    </AbsoluteFill>
  );
};

// ── Progress bar filling to `value` percent ─────────────────────────────────
export const ProgressBar: React.FC<{value: number; label?: string}> = ({value, label}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)});
  const pct = Math.max(0, Math.min(100, value)) * p;
  const w = width * 0.72;
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '20%', width: w}}>
        {label ? <div style={{...bold900, fontSize: 46, color: '#fff', textTransform: 'uppercase',
          WebkitTextStroke: '3px #000', paintOrder: 'stroke fill', marginBottom: 14}}>{label}</div> : null}
        <div style={{height: 54, borderRadius: 27, background: 'rgba(0,0,0,0.45)', border: '5px solid #000', overflow: 'hidden'}}>
          <div style={{height: '100%', width: `${pct}%`, borderRadius: 27, background: 'linear-gradient(90deg,#FFE600,#FF9D00)'}} />
        </div>
        <div style={{...bold900, fontSize: 60, color: '#FFE600', WebkitTextStroke: '3px #000', paintOrder: 'stroke fill',
          textAlign: 'right', marginTop: 8}}>{Math.round(pct)}%</div>
      </div>
    </AbsoluteFill>
  );
};

// ── Percentage ring / donut ─────────────────────────────────────────────────
export const PercentRing: React.FC<{value: number; label?: string}> = ({value, label}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)});
  const pct = Math.max(0, Math.min(100, value)) * p;
  const cx = width / 2, cy = height * 0.32, r = 150, C = 2 * Math.PI * r;
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg width="100%" height="100%">
        <g transform={`rotate(-90 ${cx} ${cy})`}>
          <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(0,0,0,0.4)" strokeWidth={34} />
          <circle cx={cx} cy={cy} r={r} fill="none" stroke="#FFE600" strokeWidth={34} strokeLinecap="round"
            strokeDasharray={C} strokeDashoffset={C * (1 - pct / 100)} />
        </g>
        <text x={cx} y={cy + 24} textAnchor="middle" style={{...bold900, fontSize: 96, fill: '#fff'}}
          stroke="#000" strokeWidth={5} paintOrder="stroke">{Math.round(pct)}%</text>
        {label ? <text x={cx} y={cy + 130} textAnchor="middle" style={{...bold900, fontSize: 46, fill: '#FFE600'}}
          stroke="#000" strokeWidth={3} paintOrder="stroke">{label.toUpperCase()}</text> : null}
      </svg>
    </AbsoluteFill>
  );
};

// ── Stat callout card ───────────────────────────────────────────────────────
export const StatCard: React.FC<{value: number; label?: string; suffix?: string}> = ({value, label, suffix}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 13, mass: 0.7}, durationInFrames: 12});
  const y = interpolate(pop, [0, 1], [80, 0]);
  const n = Math.round(value * spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)}));
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '15%', transform: `translateY(${y}px) scale(${pop})`, background: '#fff',
        borderRadius: 28, padding: '28px 44px', boxShadow: cardShadow, border: '6px solid #000', textAlign: 'center'}}>
        <div style={{...bold900, fontSize: 130, color: '#111', lineHeight: 1}}>{fmt(n)}{suffix || ''}</div>
        {label ? <div style={{...bold900, fontSize: 40, color: '#C2410C', textTransform: 'uppercase', marginTop: 6}}>{label}</div> : null}
      </div>
    </AbsoluteFill>
  );
};

export const Infographic: React.FC<{type: string; value: number; label?: string; suffix?: string}> = ({type, value, label, suffix}) => {
  switch (type) {
    case 'counter': return <Counter value={value} label={label} suffix={suffix} />;
    case 'progress': return <ProgressBar value={value} label={label} />;
    case 'ring': return <PercentRing value={value} label={label} />;
    case 'stat': return <StatCard value={value} label={label} suffix={suffix} />;
    default: return null;
  }
};
