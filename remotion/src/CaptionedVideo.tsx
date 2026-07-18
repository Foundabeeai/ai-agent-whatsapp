import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, useCurrentFrame, useVideoConfig, interpolate} from 'remotion';
import {z} from 'zod';
import {SceneBackground} from './backgrounds';
import {ArrowsRing, ScribbleCircle, Underline, BigTextBehind, LensVignette, WordCaptions} from './graphics';
import {FilmGrain, CutFlash} from './effects';

const sceneSchema = z.object({
  start: z.number(),
  end: z.number(),
  bg: z.enum(['grid', 'cardboard', 'solid', 'split', 'broll']).default('solid'),
  color: z.string().optional().default(''),
  color2: z.string().optional().default(''),
  brollSrc: z.string().optional().default(''),
  presenter: z.enum(['full', 'sticker', 'none']).default('full'),
  bigText: z.string().optional().default(''),
  doodle: z.enum(['arrows', 'circle', 'underline', 'none']).default('none'),
  zoom: z.string().optional().default('none'),
  lens: z.boolean().optional().default(false),
  emphasis: z.boolean().optional().default(false),
});

export const captionedVideoSchema = z.object({
  fps: z.number(),
  width: z.number(),
  height: z.number(),
  durationInFrames: z.number(),
  // "back"  = opaque backgrounds + big-text-behind (presenter is keyed over this by ffmpeg)
  // "front" = the composited bgVideo (back+presenter) + doodles + lens + captions + grain
  // "all"   = everything (Studio preview only)
  layer: z.enum(['all', 'back', 'front']).optional().default('all'),
  presenterSrc: z.string().optional().default(''),
  bgVideo: z.string().optional().default(''),
  scenes: z.array(sceneSchema).default([]),
  words: z.array(z.object({start: z.number(), end: z.number(), text: z.string()})).default([]),
  captionPos: z.enum(['top', 'bottom']).optional().default('bottom'),
});

export type CaptionedVideoProps = z.infer<typeof captionedVideoSchema>;

// One shot of the composited (background+presenter) video with a per-cut zoom.
// Every cut moves at least 25%; emphasis cuts punch harder.
const ZoomedShot: React.FC<{
  src: string;
  startFrom: number;
  zoom: string;
  emphasis: boolean;
  durationInFrames: number;
}> = ({src, startFrom, zoom, emphasis, durationInFrames}) => {
  const frame = useCurrentFrame();
  const AMT = emphasis ? 0.34 : 0.27; // ≥25% travel on every cut
  let from = 1.0;
  let to = 1.0 + AMT;
  if (zoom === 'out') {
    from = 1.0 + AMT;
    to = 1.0;
  } else if (zoom === 'punch' || emphasis) {
    from = 1.06;
    to = 1.06 + AMT;
  }
  const scale = interpolate(frame, [0, durationInFrames], [from, to], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <AbsoluteFill style={{overflow: 'hidden', backgroundColor: 'black'}}>
      <OffthreadVideo
        src={src}
        startFrom={startFrom}
        muted={false}
        style={{width: '100%', height: '100%', objectFit: 'cover', transform: `scale(${scale})`}}
      />
    </AbsoluteFill>
  );
};

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({scenes, words, captionPos, layer, bgVideo}) => {
  const {fps} = useVideoConfig();
  const showBack = layer === 'all' || layer === 'back';
  const showFront = layer === 'all' || layer === 'front';

  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {/* ── FRONT base: the composited back+presenter video, per-scene zoom ── */}
      {layer === 'front' && bgVideo
        ? scenes.map((s, i) => {
            const from = Math.max(0, Math.round(s.start * fps));
            const dur = Math.max(1, Math.round((s.end - s.start) * fps));
            return (
              <Sequence key={`bv${i}`} from={from} durationInFrames={dur}>
                <ZoomedShot src={bgVideo} startFrom={from} zoom={s.zoom} emphasis={s.emphasis} durationInFrames={dur} />
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
                <SceneBackground
                  bg={s.bg}
                  color={s.color || undefined}
                  color2={s.color2 || undefined}
                  brollSrc={s.brollSrc || undefined}
                  zoom={s.zoom}
                  durationInFrames={dur}
                />
                {s.bigText ? <BigTextBehind text={s.bigText} /> : null}
              </AbsoluteFill>
            </Sequence>
          );
        })}

      {/* ── FRONT: doodles + lens per scene ── */}
      {showFront &&
        scenes.map((s, i) => {
          const from = Math.max(0, Math.round(s.start * fps));
          const dur = Math.max(1, Math.round((s.end - s.start) * fps));
          if (s.doodle === 'none' && !s.lens) return null;
          return (
            <Sequence key={`fx${i}`} from={from} durationInFrames={dur}>
              <AbsoluteFill>
                {s.doodle === 'arrows' ? <ArrowsRing /> : null}
                {s.doodle === 'circle' ? <ScribbleCircle /> : null}
                {s.doodle === 'underline' ? <Underline position={captionPos} /> : null}
                {s.lens ? <LensVignette /> : null}
              </AbsoluteFill>
            </Sequence>
          );
        })}

      {/* ── FRONT: clean cut transitions ── */}
      {showFront &&
        scenes.map((s, i) => {
          if (i === 0) return null;
          const cutF = Math.round(s.start * fps);
          return (
            <Sequence key={`flash${i}`} from={Math.max(0, cutF - 4)} durationInFrames={8}>
              <CutFlash />
            </Sequence>
          );
        })}

      {/* ── FRONT: film grain + kinetic word captions ── */}
      {showFront ? <FilmGrain opacity={0.06} /> : null}
      {showFront && words && words.length ? <WordCaptions words={words} position={captionPos} /> : null}
    </AbsoluteFill>
  );
};
