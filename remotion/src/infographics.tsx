import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';
import {MONT, INTER} from './fonts';

const fmt = (n: number) => {
  if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n % 1000 ? 1 : 0) + 'K';
  return String(Math.round(n));
};

const ACCENT = '#2BE86B';
const num900: React.CSSProperties = {fontFamily: INTER, fontWeight: 900, letterSpacing: -2};
const lab800: React.CSSProperties = {fontFamily: MONT, fontWeight: 800, letterSpacing: 1};

// ── Number counter — clean, floating, with an accent label pill ─────────────
export const Counter: React.FC<{value: number; label?: string; suffix?: string}> = ({value, label, suffix}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const count = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps * 1.1)});
  const pop = spring({frame, fps, config: {damping: 12, mass: 0.6}, durationInFrames: 10});
  const n = Math.round(value * count);
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '15%', transform: `translateY(${interpolate(pop, [0, 1], [40, 0])}px) scale(${interpolate(pop, [0, 1], [0.8, 1])})`,
        textAlign: 'center', filter: 'drop-shadow(0 16px 30px rgba(0,0,0,0.5))'}}>
        <div style={{...num900, fontSize: 210, color: '#fff', lineHeight: 0.9,
          textShadow: '0 6px 0 rgba(0,0,0,0.25)'}}>
          {fmt(n)}{suffix || ''}
        </div>
        {label ? (
          <div style={{...lab800, display: 'inline-block', marginTop: 18, fontSize: 44, color: '#07110B',
            background: ACCENT, textTransform: 'uppercase', padding: '10px 26px', borderRadius: 999,
            boxShadow: '0 10px 22px rgba(0,0,0,0.3)'}}>{label}</div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

// ── Progress bar — rounded, gradient fill, floating percentage ──────────────
export const ProgressBar: React.FC<{value: number; label?: string}> = ({value, label}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)});
  const pop = spring({frame, fps, config: {damping: 13}, durationInFrames: 10});
  const pct = Math.max(0, Math.min(100, value)) * p;
  const w = width * 0.74;
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '22%', width: w, transform: `scale(${interpolate(pop, [0, 1], [0.9, 1])})`,
        filter: 'drop-shadow(0 16px 30px rgba(0,0,0,0.45))'}}>
        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 16}}>
          {label ? <span style={{...lab800, fontSize: 44, color: '#fff', textTransform: 'uppercase',
            textShadow: '0 3px 10px rgba(0,0,0,0.8)'}}>{label}</span> : <span />}
          <span style={{...num900, fontSize: 72, color: ACCENT, textShadow: '0 3px 10px rgba(0,0,0,0.7)'}}>{Math.round(pct)}%</span>
        </div>
        <div style={{height: 40, borderRadius: 999, background: 'rgba(255,255,255,0.18)', backdropFilter: 'blur(2px)', overflow: 'hidden'}}>
          <div style={{height: '100%', width: `${pct}%`, borderRadius: 999,
            background: `linear-gradient(90deg, ${ACCENT}, #17B85A)`}} />
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Percentage ring — thick gradient donut, clean center number ─────────────
export const PercentRing: React.FC<{value: number; label?: string}> = ({value, label}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)});
  const pop = spring({frame, fps, config: {damping: 13}, durationInFrames: 10});
  const pct = Math.max(0, Math.min(100, value)) * p;
  const cx = width / 2, cy = height * 0.3, r = 165, C = 2 * Math.PI * r;
  return (
    <AbsoluteFill style={{pointerEvents: 'none', transform: `scale(${interpolate(pop, [0, 1], [0.85, 1])})`,
      transformOrigin: `${cx}px ${cy}px`}}>
      <svg width="100%" height="100%">
        <defs>
          <linearGradient id="ringgrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={ACCENT} /><stop offset="100%" stopColor="#12A050" />
          </linearGradient>
        </defs>
        <g transform={`rotate(-90 ${cx} ${cy})`}>
          <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.16)" strokeWidth={40} />
          <circle cx={cx} cy={cy} r={r} fill="none" stroke="url(#ringgrad)" strokeWidth={40} strokeLinecap="round"
            strokeDasharray={C} strokeDashoffset={C * (1 - pct / 100)} style={{filter: 'drop-shadow(0 8px 16px rgba(0,0,0,0.4))'}} />
        </g>
        <text x={cx} y={cy + 34} textAnchor="middle" style={{...num900, fontSize: 130, fill: '#fff'}}>{Math.round(pct)}%</text>
      </svg>
      {label ? (
        <div style={{position: 'absolute', top: cy + r - 6, left: 0, right: 0, textAlign: 'center'}}>
          <span style={{...lab800, fontSize: 44, color: '#07110B', background: ACCENT, textTransform: 'uppercase',
            padding: '10px 26px', borderRadius: 999, boxShadow: '0 10px 22px rgba(0,0,0,0.3)'}}>{label}</span>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

// ── Stat callout card — modern glassy card with accent top bar ──────────────
export const StatCard: React.FC<{value: number; label?: string; suffix?: string}> = ({value, label, suffix}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 13, mass: 0.7}, durationInFrames: 12});
  const count = spring({frame, fps, config: {damping: 200}, durationInFrames: Math.round(fps)});
  const y = interpolate(pop, [0, 1], [90, 0]);
  const n = Math.round(value * count);
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', pointerEvents: 'none'}}>
      <div style={{marginTop: '15%', transform: `translateY(${y}px) scale(${interpolate(pop, [0, 1], [0.86, 1])})`,
        borderRadius: 34, overflow: 'hidden', boxShadow: '0 26px 60px rgba(0,0,0,0.5)', textAlign: 'center',
        background: 'rgba(255,255,255,0.96)', minWidth: 460}}>
        <div style={{height: 16, background: ACCENT}} />
        <div style={{padding: '30px 52px 38px'}}>
          <div style={{...num900, fontSize: 140, color: '#0A0A0A', lineHeight: 0.95}}>{fmt(n)}{suffix || ''}</div>
          {label ? <div style={{...lab800, fontSize: 40, color: '#6B7280', textTransform: 'uppercase', marginTop: 8}}>{label}</div> : null}
        </div>
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
