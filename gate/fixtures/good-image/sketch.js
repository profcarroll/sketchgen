// good-image — a photograph from outside the sketch, drawn once, then noLoop().
// The clean pass for loads(image) (docs/plans/media-assertion.md §3.3): the word
// exists because of entries 429 and 1103, both of which fetched a picture from
// picsum.photos and published a canvas with nothing on it.
//
// The URL is seeded — /seed/sketchgen/ — so two runs of this fixture draw the
// same photograph and the strip is comparable; an unseeded picsum URL is allowed
// by the gate and says so in resources_loaded, but it would make this fixture's
// own image a different one every time (DECIDE[image-determinism]).
//
// preload() is where it goes: p5 blocks setup() until the image arrives, so
// there is no first frame with a hole in it, and the gate waits out the whole
// --timeout for the canvas when loads(image) is asserted (DECIDE[image-wait]).

let photo;

function preload() {
  photo = loadImage('https://picsum.photos/seed/sketchgen/400/300');
}

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  imageMode(CENTER);
  noStroke();
}

function draw() {
  background(12);

  // Cover about two thirds of the shorter side, keeping the 4:3 the host served.
  const h = min(width, height) * 0.66;
  const w = h * (4 / 3);
  image(photo, width / 2, height / 2, w, h);

  noLoop();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  redraw();
}
