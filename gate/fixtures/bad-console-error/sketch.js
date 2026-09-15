// bad-console-error — renders correctly for ten frames, then calls a function that
// was never defined. The failure is late, so a gate that only looks at load time
// misses it.

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  noStroke();
}

function draw() {
  background(12);
  fill(200, 90, 60);
  ellipse(width / 2 + sin(frameCount * 0.05) * 120, height / 2, 60, 60);

  if (frameCount > 10) {
    tumbleTheCanvas();
  }
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
