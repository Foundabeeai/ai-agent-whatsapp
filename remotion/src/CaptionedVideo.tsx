import React from 'react';
import {
  AbsoluteFill,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
} from 'remotion';
import {z} from 'zod';

export const captionedVideoSchema = z.object({
  videoSrc: z.string(),
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  durationInFrames: z.number(),
  title: z.string().optional().default(''),
  cta: z.string().optional().default(''),
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

const HIGHLIGHT = '#FFE600'; // punchy yellow for emphasis words

// ── One caption line, springs in from the bottom ──────────────────────────
const CaptionCue: React.FC<{text: string; emphasis: boolean}> = ({text, emphasis}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 14, stiffness: 200, mass: 0.6}, durationInFrames: 10});
  const scale = interpolate(pop, [0, 1], [0.7, 1]);

  return (
    <AbsoluteFill
      style={{
        justifyContent: 'flex-end',
        alignItems: 'center',
        paddingBottom: 320,
        paddingLeft: 70,
        paddingRight: 70,
      }}
    >
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
          textShadow: '0 0 4px #000, 0 6px 18px rgba(0,0,0,0.85), 0 0 2px #000',
          WebkitTextStroke: '3px #000',
          paintOrder: 'stroke fill',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// ── Opening title card (first ~1.4s) ──────────────────────────────────────
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

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({videoSrc, captions, title, fps}) => {
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {videoSrc ? <OffthreadVideo src={videoSrc} /> : null}

      {title ? (
        <Sequence from={0} durationInFrames={Math.round(1.6 * fps)}>
          <TitleCard title={title} />
        </Sequence>
      ) : null}

      {captions.map((c, i) => {
        const from = Math.max(0, Math.round(c.start * fps));
        const dur = Math.max(1, Math.round((c.end - c.start) * fps));
        return (
          <Sequence key={i} from={from} durationInFrames={dur}>
            <CaptionCue text={c.text} emphasis={!!c.emphasis} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
