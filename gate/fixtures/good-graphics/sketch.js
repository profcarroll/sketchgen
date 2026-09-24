// good-graphics — a looping sketch that paints into createGraphics() buffers.
// Job 1542 (entry 1531, 2026-09-24) made two gradient buffers this way and never
// called noLoop(), and the gate reported is_looping false and skipped
// frame_advancing as if it had. p5 1.11's Graphics constructor runs
// p5.prototype._initializeInstanceVariables on the buffer itself, and the gate's
// hook on that method kept whatever it was last called with, so every runtime
// read came off the second buffer: no draw loop, a frameCount stuck at 0, and
// the buffer's own width and height. The same sketch with
// document.createElement('canvas') buffers read true and true.
//
// The canvas is a fixed 640x480 and neither buffer is, so size(640,480) reads
// the sketch and not the last buffer made.

let sky, glow;
let t = 0;

function setup() {
  createCanvas(640, 480);
  randomSeed(1);
  noiseSeed(1);

  // A vertical gradient one strip wide, stretched across the canvas each frame.
  sky = createGraphics(32, 480);
  for (let y = 0; y < sky.height; y++) {
    sky.stroke(lerpColor(color(18, 24, 60), color(230, 120, 70), y / sky.height));
    sky.line(0, y, sky.width, y);
  }

  // A soft round glow, the last buffer made: 160x160.
  glow = createGraphics(160, 160);
  glow.noStroke();
  for (let r = 80; r > 0; r -= 4) {
    glow.fill(255, 230, 160, 12);
    glow.ellipse(80, 80, r * 2, r * 2);
  }
}

function draw() {
  image(sky, 0, 0, width, height);
  t += 0.02;
  image(glow, width * noise(t) - 80, height * noise(t + 50) - 80);
}
