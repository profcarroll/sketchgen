// good-2d-webgl-buffer — the reverse of good-webgl-2d-buffer: a 2D sketch that
// renders one solid into a WEBGL createGraphics() buffer and places it on a flat
// composition. uses(webgl) is about the sketch's own renderer, and this sketch's
// is 2D; until 2026-09-24 the gate read the buffer's instead and said WebGL (see
// good-graphics). The buffer is 200x200, so size(640,480) is read wrong the
// same way.

let solid;
let t = 0;

function setup() {
  createCanvas(640, 480);
  randomSeed(1);
  noiseSeed(1);
  solid = createGraphics(200, 200, WEBGL);
}

function draw() {
  background(230, 220, 200);
  t += 0.02;

  solid.clear();
  solid.push();
  solid.rotateX(t * 0.7);
  solid.rotateY(t);
  solid.normalMaterial();
  solid.box(90);
  solid.pop();

  image(solid, width / 2 - 100 + 160 * sin(t), height / 2 - 100);
}
