// good-webgl-2d-buffer — a WEBGL sketch whose texture is a 2D createGraphics()
// buffer, made after the canvas. uses(webgl) reads the renderer the gate took to
// be the sketch's, and until 2026-09-24 that was the last p5.Graphics made (see
// good-graphics): this sketch, WebGL through and through, missed uses(webgl)
// because its texture was painted in 2D.

let tex;
let t = 0;

function setup() {
  createCanvas(640, 480, WEBGL);
  randomSeed(1);
  noiseSeed(1);

  // A checkerboard, painted once.
  tex = createGraphics(128, 128);
  tex.noStroke();
  for (let i = 0; i < 8; i++) {
    for (let j = 0; j < 8; j++) {
      tex.fill((i + j) % 2 ? 235 : 40, 110, 190);
      tex.rect(i * 16, j * 16, 16, 16);
    }
  }
}

function draw() {
  background(18);
  t += 0.02;
  rotateX(t * 0.7);
  rotateY(t);
  noStroke();
  texture(tex);
  box(180);
}
