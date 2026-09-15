// bad-frozen — the loop runs, the console is clean, and every frame is identical.
// It never calls noLoop(), so it makes no claim to be static: isLooping() is true and
// frameCount climbs. "Running correctly on silence" — no fixed check can see this,
// only the motion(idle) frame-diff assertion can.

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  noStroke();
}

function draw() {
  background(18);

  fill(120);
  rect(width / 2 - 100, height / 2 - 100, 200, 200);

  fill(240);
  ellipse(width / 2, height / 2, 80, 80);
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
