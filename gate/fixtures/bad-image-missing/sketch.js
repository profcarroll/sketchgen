// bad-image-missing — entry 429's run, on purpose. The sketch reaches outside
// itself for a picture, the picture is not there, and nothing in the console
// says so: a failed image is not a page error, so console_clean stays true and
// the canvas is a flat background nobody can explain. That is the whole reason
// ResourceLog exists and the whole reason loads(image) can fail.
//
// The host is cdnjs, the one every fixture here already needs for p5 itself:
// if this URL cannot be reached at all then no fixture in this directory can
// run, so the 404 this asks for is as reliable as the harness. The path is a
// file that has never existed under a real, CORS-serving library directory.
//
// The failure callback is deliberate and it is not a fallback. Without one, p5
// logs the error itself, and this fixture would be caught by console_clean
// instead of by the assertion — which would make it a test of something else.
// With one, preload() finishes, setup() runs, and the sketch draws exactly
// what entry 429 published: the background, and no photograph.

let photo = null;
let missing = false;

function preload() {
  photo = loadImage(
    'https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/no-such-image.png',
    function () { missing = false; },
    function () { missing = true; }
  );
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

  if (!missing && photo && photo.width > 1) {
    const h = min(width, height) * 0.66;
    image(photo, width / 2, height / 2, h * (4 / 3), h);
  }

  noLoop();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  redraw();
}
