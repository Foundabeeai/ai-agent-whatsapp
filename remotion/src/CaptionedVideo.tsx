import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, useVideoConfig} from 'remotion';
import {z} from 'zod';
import {SceneBackground} from './backgrounds';
import {ArrowsRing, ScribbleCircle, BigTextBehind, LensVignette, WordCaptions} from './graphics';
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
  doodle: z.enum(['arrows', 'circle', 'none']).default('none'),
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

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({scenes, words, captionPos, layer, bgVideo}) => {
  const {fps} = useVideoConfig();
  const showBack = layer === 'all' || layer === 'back';
  const showFront = layer === 'all' || layer === 'front';

  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {/* ── FRONT base: the composited back+presenter video (opaque) ── */}
      {layer === 'front' && bgVideo ? (
        <OffthreadVideo src={bgVideo} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
      ) : null}

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
