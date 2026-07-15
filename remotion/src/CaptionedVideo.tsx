import React from 'react';
import {
  AbsoluteFill,
  Audio,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
} from 'remotion';
import {z} from 'zod';

export const captionedVideoSchema = z.object({
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  durationInFrames: z.number(),
  audioSrc: z.string().optional().default(''),
  presenterSrc: z.string().optional().default(''),
  title: z.string().optional().default(''),
  cta: z.string().optional().default(''),
  broll: z
    .array(
      z.object({
        start: z.number(),
        end: z.number(),
        src: z.string(),
        zoom: z.string().optional().default('none'),
      })
    )
    .default([]),
  captions: z
    .array(
      z.object({
        start: z.number(),
        end: z.number(),
        text: z.string(),
        emphasis: z.boolean().optional().default(false),
      })
    )
    .default([]),
});

export type CaptionedVideoProps = z.infer<typeof captionedVideoSchema>;

const HIGHLIGHT = '#FFE600';

// ── One B-roll shot: cover-fills the frame + slow Ken Burns zoom for motion ──
const BrollShot: React.FC<{src: string; zoom: string; durationInFrames: number}> = ({
  src,
  zoom,
  durationInFrames,
}) => {
  const frame = useCurrentFrame();
  // Every shot gets movement; direction depends on the plan's zoom hint.
  const from = zoom === 'out' ? 1.15 : 1.0;
  const to = zoom === 'out' ? 1.0 : zoom === 'in' ? 1.15 : 1.08;
  const scale = interpolate(frame, [0, durationInFrames], [from, to], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      <OffthreadVideo
        src={src}
        muted
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          transform: `scale(${scale})`,
        }}
      />
    </AbsoluteFill>
  );
};

// ── Presenter: transparent WebM, springs up on entry, gentle breathing scale ──
const Presenter: React.FC<{src: string}> = ({src}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, mass: 0.7}, durationInFrames: 14});
  const y = interpolate(enter, [0, 1], [90, 0]);
  const breathe = 1 + 0.01 * Math.sin(frame / 24);
  return (
    <AbsoluteFill style={{transform: `translateY(${y}px) scale(${breathe})`}}>
      <OffthreadVideo
        src={src}
        transparent
        style={{width: '100%', height: '100%', objectFit: 'cover'}}
      />
    </AbsoluteFill>
  );
};

const CaptionCue: React.FC<{text: string; emphasis: boolean}> = ({text, emphasis}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 14, stiffness: 200, mass: 0.6}, durationInFrames: 10});
  const scale = interpolate(pop, [0, 1], [0.7, 1]);
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 320, paddingLeft: 70, paddingRight: 70}}>
      <div
        style={{
          transform: `scale(${scale})`,
          fontFamily: 'Arial Black, Arial, sans-serif',
          fontWeight: 900,
          fontSize: 76,
          lineHeight: 1.05,
          letterSpacing: -1,
          textAlign: 'center',
          textTransform: 'uppercase',
          color: emphasis ? HIGHLIGHT : 'white',
          textShadow: '0 0 4px #000, 0 6px 18px rgba(0,0,0,0.85)',
          WebkitTextStroke: '3px #000',
          paintOrder: 'stroke fill',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

const TitleCard: React.FC<{title: string}> = ({title}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const appear = spring({frame, fps, config: {damping: 16}, durationInFrames: 12});
  const opacity = interpolate(frame, [0, 8, 30, 40], [0, 1, 1, 0], {extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center', paddingTop: 180}}>
      <div
        style={{
          opacity,
          transform: `translateY(${interpolate(appear, [0, 1], [-40, 0])}px)`,
          fontFamily: 'Arial Black, Arial, sans-serif',
          fontWeight: 900,
          fontSize: 64,
          color: 'white',
          textAlign: 'center',
          padding: '0 60px',
          textShadow: '0 4px 16px rgba(0,0,0,0.9)',
          WebkitTextStroke: '2px #000',
          paintOrder: 'stroke fill',
        }}
      >
        {title}
      </div>
    </AbsoluteFill>
  );
};

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({
  broll,
  presenterSrc,
  audioSrc,
  captions,
  title,
  fps,
}) => {
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {/* ── Layer 1: B-roll timeline with hard cuts + Ken Burns ── */}
      {broll.map((b, i) => {
        const from = Math.max(0, Math.round(b.start * fps));
        const dur = Math.max(1, Math.round((b.end - b.start) * fps));
        return (
          <Sequence key={`b${i}`} from={from} durationInFrames={dur}>
            <BrollShot src={b.src} zoom={b.zoom} durationInFrames={dur} />
          </Sequence>
        );
      })}

      {/* ── Layer 2: transparent presenter on top ── */}
      {presenterSrc ? <Presenter src={presenterSrc} /> : null}

      {/* ── Audio from the original recording ── */}
      {audioSrc ? <Audio src={audioSrc} /> : null}

      {/* ── Layer 3: title card ── */}
      {title ? (
        <Sequence from={0} durationInFrames={Math.round(1.6 * fps)}>
          <TitleCard title={title} />
        </Sequence>
      ) : null}

      {/* ── Layer 4: captions ── */}
      {captions.map((c, i) => {
        const from = Math.max(0, Math.round(c.start * fps));
        const dur = Math.max(1, Math.round((c.end - c.start) * fps));
        return (
          <Sequence key={`c${i}`} from={from} durationInFrames={dur}>
            <CaptionCue text={c.text} emphasis={!!c.emphasis} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
