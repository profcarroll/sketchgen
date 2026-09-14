// good-motion — moves every frame with no interaction, deterministic under fixed
// seeds, and changes colour on mousePressed. The gate's "passes cleanly" reference.

let hueBase = 0;
let t = 0;

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  colorMode(HSB, 360, 100, 100);
  noStroke();
}

function draw() {
  background(220, 30, 12);
  t += 0.02;

  for (let i = 0; i < 12; i++) {
    let x = width * noise(i * 0.3, t);
    let y = height * (0.2 + 0.6 * noise(i * 0.3 + 50, t));
    let d = 30 + 30 * noise(i * 0.3 + 100, t);
    fill((hueBase + i * 20) % 360, 80, 90);
    ellipse(x, y, d, d);
  }
}

function mousePressed() {
  hueBase = (hueBase + 120) % 360;
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
