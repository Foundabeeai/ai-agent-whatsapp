import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, useCurrentFrame, useVideoConfig, interpolate} from 'remotion';
import {z} from 'zod';
import {SceneBackground} from './backgrounds';
import {BigTextBehind, LensVignette, WordCaptions} from './graphics';
import {Doodle, EmojiPop} from './doodles';
import {Infographic} from './infographics';
import {FilmGrain, CutFlash, WhipSwipe, GlitchBurst, useShake} from './effects';

const DOODLES = ['none', 'arrow', 'arrows', 'circle', 'underline', 'highlighter', 'box', 'brackets', 'stars', 'action_lines', 'check', 'cross'] as const;
const INFO_TYPES = ['none', 'counter', 'progress', 'ring', 'stat'] as const;
const TRANSITIONS = ['none', 'flash', 'whip', 'glitch', 'shake'] as const;

const sceneSchema = z.object({
  start: z.number(),
  end: z.number(),
  bg: z.enum(['grid', 'cardboard', 'solid', 'split', 'broll']).default('solid'),
  color: z.string().optional().default(''),
  color2: z.string().optional().default(''),
  brollSrc: z.string().optional().default(''),
  presenter: z.enum(['full', 'sticker', 'none']).default('full'),
  bigText: z.string().optional().default(''),
  doodle: z.enum(DOODLES).default('none'),
  emoji: z.string().optional().default(''),
  info: z.object({
    type: z.enum(INFO_TYPES).default('none'),
    value: z.number().optional().default(0),
    label: z.string().optional().default(''),
    suffix: z.string().optional().default(''),
  }).optional(),
  transition: z.enum(TRANSITIONS).optional().default('flash'),
  zoom: z.string().optional().default('none'),
  lens: z.boolean().optional().default(false),
  emphasis: z.boolean().optional().default(false),
});

export const captionedVideoSchema = z.object({
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  durationInFrames: z.number(),
  layer: z.enum(['all', 'back', 'front']).optional().default('all'),
  presenterSrc: z.string().optional().default(''),
  bgVideo: z.string().optional().default(''),
  scenes: z.array(sceneSchema).default([]),
  words: z.array(z.object({start: z.number(), end: z.number(), text: z.string()})).default([]),
  captionPos: z.enum(['top', 'bottom']).optional().default('bottom'),
});

export type CaptionedVideoProps = z.infer<typeof captionedVideoSchema>;

// One shot of the composited (background+presenter) video with a per-cut zoom
// (≥25% travel) and an optional shake on a hard-cut transition.
const ZoomedShot: React.FC<{
  src: string;
  startFrom: number;
  zoom: string;
  emphasis: boolean;
  shake: boolean;
  durationInFrames: number;
}> = ({src, startFrom, zoom, emphasis, shake, durationInFrames}) => {
  const frame = useCurrentFrame();
  const AMT = emphasis ? 0.34 : 0.27;
  let from = 1.0;
  let to = 1.0 + AMT;
  if (zoom === 'out') {
    from = 1.0 + AMT;
    to = 1.0;
  } else if (zoom === 'punch' || emphasis) {
    from = 1.06;
    to = 1.06 + AMT;
  }
  const scale = interpolate(frame, [0, durationInFrames], [from, to], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const sh = useShake(shake ? 10 : 0);
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      <OffthreadVideo
        src={src}
        startFrom={startFrom}
        muted={false}
        style={{width: '100%', height: '100%', objectFit: 'cover', transform: `translate(${sh.x}px, ${sh.y}px) scale(${scale})`}}
      />
    </AbsoluteFill>
  );
};

const TransitionFx: React.FC<{kind: string}> = ({kind}) => {
  if (kind === 'whip') return <WhipSwipe />;
  if (kind === 'glitch') return <GlitchBurst />;
  if (kind === 'flash') return <CutFlash />;
  return null; // 'shake' handled inside ZoomedShot; 'none' → nothing
};

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({scenes, words, captionPos, layer, bgVideo}) => {
  const {fps} = useVideoConfig();
  const showBack = layer === 'all' || layer === 'back';
  const showFront = layer === 'all' || layer === 'front';

  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {/* ── FRONT base: composited back+presenter video, per-scene zoom/shake ── */}
      {layer === 'front' && bgVideo
        ? scenes.map((s, i) => {
            const from = Math.max(0, Math.round(s.start * fps));
            const dur = Math.max(1, Math.round((s.end - s.start) * fps));
            return (
              <Sequence key={`bv${i}`} from={from} durationInFrames={dur}>
                <ZoomedShot src={bgVideo} startFrom={from} zoom={s.zoom} emphasis={s.emphasis}
                  shake={s.transition === 'shake'} durationInFrames={dur} />
              </Sequence>
            );
          })
        : null}

      {/* ── BACK: designed backgrounds + giant text behind the subject ── */}
      {showBack &&
        scenes.map((s, i) => {
          const from = Math.max(0, Math.round(s.start * fps));
          const dur = Math.max(1, Math.round((s.end - s.start) * fps));
          return (
            <Sequence key={`bg${i}`} from={from} durationInFrames={dur}>
              <AbsoluteFill>
                <SceneBackground bg={s.bg} color={s.color || undefined} color2={s.color2 || undefined}
                  brollSrc={s.brollSrc || undefined} zoom={s.zoom} durationInFrames={dur} />
                {s.bigText ? <BigTextBehind text={s.bigText} /> : null}
              </AbsoluteFill>
            </Sequence>
          );
        })}

      {/* ── FRONT: doodles + infographic + emoji + lens per scene ── */}
      {showFront &&
        scenes.map((s, i) => {
          const from = Math.max(0, Math.round(s.start * fps));
          const dur = Math.max(1, Math.round((s.end - s.start) * fps));
          const info = s.info && s.info.type !== 'none' ? s.info : null;
          if (s.doodle === 'none' && !s.lens && !s.emoji && !info) return null;
          return (
            <Sequence key={`fx${i}`} from={from} durationInFrames={dur}>
              <AbsoluteFill>
                {s.doodle !== 'none' ? <Doodle kind={s.doodle} captionPos={captionPos} /> : null}
                {info ? <Infographic type={info.type} value={info.value || 0} label={info.label} suffix={info.suffix} /> : null}
                {s.emoji ? <EmojiPop emoji={s.emoji} /> : null}
                {s.lens ? <LensVignette /> : null}
              </AbsoluteFill>
            </Sequence>
          );
        })}

      {/* ── FRONT: per-cut transition (whip / glitch / flash) ── */}
      {showFront &&
        scenes.map((s, i) => {
          if (i === 0) return null;
          const cutF = Math.round(s.start * fps);
          const kind = s.transition || 'flash';
          if (kind === 'none' || kind === 'shake') return null;
          const len = kind === 'whip' ? 10 : 8;
          return (
            <Sequence key={`tr${i}`} from={Math.max(0, cutF - Math.floor(len / 2))} durationInFrames={len}>
              <TransitionFx kind={kind} />
            </Sequence>
          );
        })}

      {/* ── FRONT: film grain + kinetic word captions ── */}
      {showFront ? <FilmGrain opacity={0.06} /> : null}
      {showFront && words && words.length ? <WordCaptions words={words} position={captionPos} /> : null}
    </AbsoluteFill>
  );
};
