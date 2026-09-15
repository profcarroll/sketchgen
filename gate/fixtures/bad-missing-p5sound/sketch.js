// bad-missing-p5sound — the bug the 30B found by reading: audio classes used with
// no p5.sound addon tag in index.html. Throws in setup(), so the console is dirty
// and frameCount never advances.

let mic;
let fft;

function setup() {
  createCanvas(windowWidth, windowHeight);
  noStroke();

  mic = new p5.AudioIn();
  mic.start();

  fft = new p5.FFT(0.8, 1024);
  fft.setInput(mic);
}

function draw() {
  background(12);
  let level = fft.getEnergy("bass");
  fill(255);
  ellipse(width / 2, height / 2, 50 + level, 50 + level);
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
