// ghost-echo — nothing happens until it is touched, and then it keeps the mark.
// It is entry 1103's shape with the puzzle taken out: no_motion, a dot where it
// is clicked, a line where it is dragged. Idle, the canvas never changes, so the
// strip is four identical frames and a judge learns nothing from it; under the
// ghost pointer the same sketch draws the script it was given, which is the whole
// claim ghost.png makes (docs/plans/auto-mouse.md §5, 2026-09-21).
//
// The marks are painted straight onto the canvas in the handlers, never in
// draw(), which is what keeps no_motion true: draw() touches nothing, so the
// idle window's first and last frames are the same pixels.

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(1);
  noiseSeed(1);
  background(12);
  stroke(255, 200, 80);
  strokeWeight(3);
  noFill();
}

function draw() {
  // Deliberately empty. frameCount still advances, so frame_advancing is true
  // and the sketch has not declared itself static: it is waiting.
}

function mousePressed() {
  fill(255, 200, 80);
  ellipse(mouseX, mouseY, 28, 28);
  noFill();
}

function mouseDragged() {
  line(pmouseX, pmouseY, mouseX, mouseY);
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  background(12);
}
