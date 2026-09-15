// good-static-noloop — one composition, drawn once, then noLoop(). Deliberately
// still and correct: the gate must not read stillness as failure when the sketch
// declares itself static through p5's own API.

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  colorMode(HSB, 360, 100, 100);
  noStroke();
}

function draw() {
  background(220, 30, 12);

  let r = min(width, height) * 0.3;
  for (let i = 0; i < 60; i++) {
    let a = (TWO_PI * i) / 60;
    fill((i * 6) % 360, 70, 85);
    ellipse(width / 2 + cos(a) * r, height / 2 + sin(a) * r, 24, 24);
  }

  fill(0, 0, 100);
  ellipse(width / 2, height / 2, 40, 40);

  noLoop();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  redraw();
}
