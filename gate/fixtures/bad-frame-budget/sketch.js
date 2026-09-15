// bad-frame-budget — the job 166 failure with nothing else wrong with it.
//
// Every fixed check the gate had before 2026-09-15 passes here: the console is
// clean, isLooping() is true, frameCount advances, there is no p5.sound, and
// motion(idle) sees the canvas change. The only thing wrong is the cost: one
// frame of this takes about a third of a second, which is three frames' worth
// of budget for one frame of picture, and 120 of them is the two minutes a gate
// run must never take. That is the whole bug entry 165 shipped with.
//
// The burn is deliberately arithmetic rather than drawing. p5's own WEBGL
// immediate mode is what actually cost job 166 its frames, but a fixture that
// leaned on the GPU would measure the machine's driver instead of the gate, and
// SwiftShader on the node and a real GPU on a laptop are not the same test.
// Scalar float maths in the same V8 that runs every other sketch is.
//
// CALIBRATION. 30,000,000 iterations of the loop below measured ~310 ms on the
// operator's laptop (2026-09-15) and more than that on the node, whose cores are
// slower. The budget it has to break is 100 ms a frame, so there is a factor of
// three in hand on the fastest machine in this project and more everywhere else.
// If this fixture ever stops failing, hardware got quicker than the number and
// the number is what to change — not the budget.
const BURN = 30000000;

let t = 0;
let sink = 0;

function setup() {
  createCanvas(640, 400);
  randomSeed(1);
  noiseSeed(1);
  colorMode(HSB, 360, 100, 100);
  noStroke();
}

function draw() {
  background(220, 30, 12);
  t += 0.02;

  // The cost. `sink` is read by the drawing below so that no engine is entitled
  // to decide this loop does nothing and delete it.
  let s = 0;
  for (let i = 0; i < BURN; i++) {
    s += Math.sqrt(i % 997) * Math.sin(i % 311);
  }
  sink = s;

  // Something moves, so motion(idle) and frame_advancing are both honestly true
  // and frame_budget is the only check with anything to say.
  for (let i = 0; i < 12; i++) {
    let x = width * noise(i * 0.3, t);
    let y = height * (0.2 + 0.6 * noise(i * 0.3 + 50, t));
    fill((i * 20 + (sink % 1) * 360) % 360, 80, 90);
    ellipse(x, y, 40, 40);
  }
}
