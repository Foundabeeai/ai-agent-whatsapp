import React from 'react';
import {Composition} from 'remotion';
import {CaptionedVideo, captionedVideoSchema} from './CaptionedVideo';

// The Python render layer passes real props via --props (JSON). These defaults
// only matter for the Remotion Studio preview.
const DEFAULT_PROPS = {
  fps: 24,
  width: 1080,
  height: 1920,
  durationInFrames: 360,
  layer: 'all' as const,
  presenterSrc: '',
  captionPos: 'bottom' as const,
  scenes: [] as unknown[],
  words: [] as {start: number; end: number; text: string}[],
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="CaptionedVideo"
      component={CaptionedVideo}
      schema={captionedVideoSchema}
      defaultProps={DEFAULT_PROPS}
      // Duration / dimensions come from the incoming props.
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.round(props.durationInFrames)),
        fps: props.fps || 24,
        width: props.width || 1080,
        height: props.height || 1920,
      })}
    />
  );
};
