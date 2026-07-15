import React from 'react';
import {AbsoluteFill, Audio, Sequence, useVideoConfig} from 'remotion';
import {z} from 'zod';
import {SceneBackground} from './backgrounds';
import {ArrowsRing, ScribbleCircle, BigTextBehind, Presenter, LensVignette, WordCaptions} from './graphics';
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
  audioSrc: z.string().optional().default(''),
  presenterSrc: z.string().optional().default(''),
  title: z.string().optional().default(''),
  cta: z.string().optional().default(''),
  scenes: z.array(sceneSchema).default([]),
  words: z.array(z.object({start: z.number(), end: z.number(), text: z.string()})).default([]),
  captionPos: z.enum(['top', 'bottom']).optional().default('bottom'),
});

export type CaptionedVideoProps = z.infer<typeof captionedVideoSchema>;

const Scene: React.FC<{scene: z.infer<typeof sceneSchema>; presenterSrc: string; durationInFrames: number}> = ({
  scene,
  presenterSrc,
  durationInFrames,
}) => {
  const punch = scene.zoom === 'punch' || scene.emphasis;
  return (
    <AbsoluteFill>
      {/* background plate */}
      <SceneBackground
        bg={scene.bg}
        color={scene.color || undefined}
        color2={scene.color2 || undefined}
        brollSrc={scene.brollSrc || undefined}
        zoom={scene.zoom}
        durationInFrames={durationInFrames}
      />
      {/* giant word behind the subject */}
      {scene.bigText ? <BigTextBehind text={scene.bigText} /> : null}
      {/* the presenter */}
      {scene.presenter !== 'none' && presenterSrc ? (
        <Presenter src={presenterSrc} mode={scene.presenter} punch={punch} />
      ) : null}
      {/* doodle overlays on top of the subject */}
      {scene.doodle === 'arrows' ? <ArrowsRing /> : null}
      {scene.doodle === 'circle' ? <ScribbleCircle /> : null}
      {/* scope look */}
      {scene.lens ? <LensVignette /> : null}
    </AbsoluteFill>
  );
};

export const CaptionedVideo: React.FC<CaptionedVideoProps> = ({
  scenes,
  presenterSrc,
  audioSrc,
  words,
  captionPos,
}) => {
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {/* ── Scenes: fast hard cuts, each its own designed background ── */}
      {scenes.map((s, i) => {
        const from = Math.max(0, Math.round(s.start * fps));
        const dur = Math.max(1, Math.round((s.end - s.start) * fps));
        return (
          <Sequence key={`s${i}`} from={from} durationInFrames={dur}>
            <Scene scene={s} presenterSrc={presenterSrc} durationInFrames={dur} />
          </Sequence>
        );
      })}

      {/* ── Clean cut transitions: quick light sweep on each boundary ── */}
      {scenes.map((s, i) => {
        if (i === 0) return null;
        const cutF = Math.round(s.start * fps);
        return (
          <Sequence key={`flash${i}`} from={Math.max(0, cutF - 4)} durationInFrames={8}>
            <CutFlash />
          </Sequence>
        );
      })}

      {/* ── Subtle film grain over everything ── */}
      <FilmGrain opacity={0.06} />

      {/* ── Audio from the original recording ── */}
      {audioSrc ? <Audio src={audioSrc} /> : null}

      {/* ── Word-by-word kinetic captions ── */}
      {words && words.length ? <WordCaptions words={words} position={captionPos} /> : null}
    </AbsoluteFill>
  );
};
