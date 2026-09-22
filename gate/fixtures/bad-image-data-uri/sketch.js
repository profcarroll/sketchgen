// bad-image-data-uri — the sketch that carried its own picture. The gate's
// README remembers it: among the 39 clean attempts thrown away for an assertion
// was one that "built its own image as a data: URI when it could not fetch
// one". That is resourceful, and it is not what loads(image) asks for: the word
// says the sketch fetched a raster image from a host outside itself
// (DECIDE[image-pass]), and a data: URI is not the web.
//
// So this fixture draws a real image, scaled up and unsmoothed into four
// quadrants, and it draws it from two bytes of base64 that travelled with the
// sketch. Every fixed check passes; the canvas is not flat; nothing failed to
// arrive. Only loads(image) knows the difference, and its detail has to say
// which difference it is — not "no image arrived", which would send the next
// attempt looking for a network fault that is not there.
//
// The PNG is 2x2, four colours, 79 bytes.

const TILE =
  'data:image/png;base64,' +
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR42mO4Y2NjU3GH4cMJ' +
  'G40FeQAqrAYPXE+fLwAAAABJRU5ErkJggg==';

let tile;

function preload() {
  tile = loadImage(TILE);
}

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  noSmooth();
  imageMode(CENTER);
  noStroke();
}

function draw() {
  background(12);

  const h = min(width, height) * 0.66;
  image(tile, width / 2, height / 2, h, h);

  noLoop();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  redraw();
}
