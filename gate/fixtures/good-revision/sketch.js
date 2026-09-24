// good-revision — good-motion revised as a critic might ask: "the same drifting
// discs, and this time each one trails a faint ring that swells as it moves."
// Seven lines of code change or arrive, over the floor of five, so `revised` is
// true and the run passes like good-motion itself.

let hueBase = 0;
let t = 0;

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  colorMode(HSB, 360, 100, 100);
}

function draw() {
  background(220, 30, 12);
  t += 0.02;

  for (let i = 0; i < 12; i++) {
    let x = width * noise(i * 0.3, t);
    let y = height * (0.2 + 0.6 * noise(i * 0.3 + 50, t));
    let d = 30 + 30 * noise(i * 0.3 + 100, t);
    let ring = d * (1.4 + 0.4 * sin(t * 3 + i));
    noFill();
    stroke((hueBase + i * 20) % 360, 60, 100, 0.35);
    strokeWeight(2);
    ellipse(x, y, ring, ring);
    noStroke();
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
