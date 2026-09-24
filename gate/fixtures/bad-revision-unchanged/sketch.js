// bad-revision-unchanged — good-motion handed back as its own revision, the way
// job 1653 handed back entry 1531 and entry 1574 handed back 1424 with one loop
// bound moved. Every comment here is new, the blank lines and the spacing are
// not good-motion's, and one number changed: one line of code, under the floor
// of five, so `revised` is false while every other check passes.

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
  background(220, 30, 12);   // a deep night blue
  t += 0.03;                 /* a touch faster */

  for (let i = 0; i < 12; i++) {
    let x = width * noise(i * 0.3, t);
    let y = height * (0.2 + 0.6 * noise(i * 0.3 + 50, t));
    let d = 30 + 30 * noise(i * 0.3 + 100, t);
    fill((hueBase + i * 20) % 360, 80, 90);
    ellipse(x,y,d,d);
  }
}

function mousePressed() {
  // rotate the palette a third of the way round
  hueBase = (hueBase + 120) % 360;
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
