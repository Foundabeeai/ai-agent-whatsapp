import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
Config.setConcurrency(2);
// Transparent-WebM frames + network media can be slow to load on the server;
// give delayRender() much longer than the 28s default so it doesn't abort.
Config.setDelayRenderTimeoutInMilliseconds(120000);
