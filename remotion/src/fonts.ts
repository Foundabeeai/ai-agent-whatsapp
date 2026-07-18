// Professional display typography — the single biggest upgrade from "template"
// to "elite creator" edits. Loaded via @remotion/google-fonts so they render
// reliably in headless renders (no external fetch at frame time).
import {loadFont as loadAnton} from '@remotion/google-fonts/Anton';
import {loadFont as loadMontserrat} from '@remotion/google-fonts/Montserrat';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';

// Anton: tall condensed display — for the giant text behind the subject.
export const ANTON = loadAnton().fontFamily;

// Montserrat 800/900: the punchy, rounded-yet-tight look of viral captions.
export const MONT = loadMontserrat('normal', {weights: ['700', '800', '900']}).fontFamily;

// Inter 900: clean numerals for infographics.
export const INTER = loadInter('normal', {weights: ['700', '800', '900']}).fontFamily;
