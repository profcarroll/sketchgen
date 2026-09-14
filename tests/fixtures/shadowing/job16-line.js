let lines = [];
let time = 0;
let clickTime = 0;
let glow = 0;

function setup() {
  createCanvas(windowWidth, windowHeight);
  noFill();
  strokeWeight(1);

  // Create lattice of lines
  for (let i = 0; i < 200; i++) {
    lines.push({
      x1: random(width),
      y1: random(height),
      x2: random(width),
      y2: random(height),
      speed: random(0.0005, 0.002),
      amp: random(5, 30),
      phase: random(TWO_PI)
    });
  }
}

function draw() {
  background(240, 230, 210); // ochre background

  time += 0.001;
  
  // Draw lines with subtle movement
  for (let line of lines) {
    let x1 = line.x1 + sin(time * line.speed + line.phase) * line.amp;
    let y1 = line.y1 + cos(time * line.speed + line.phase) * line.amp;
    let x2 = line.x2 + sin(time * line.speed + line.phase + PI/2) * line.amp;
    let y2 = line.y2 + cos(time * line.speed + line.phase + PI/2) * line.amp;

    // Set color with earth tones
    let hue = map(sin(time * 0.01), -1, 1, 20, 40); // ochre to sienna
    let sat = 30;
    let bright = 60 + sin(time * 0.005) * 10;
    
    stroke(hue, sat, bright);
    
    // Flash effect on click
    if (millis() - clickTime < 300) {
      let flash = map(millis() - clickTime, 0, 300, 255, 0);
      stroke(255, 255, 200, flash * 0.7);
      glow = max(glow, flash);
    } else {
      glow *= 0.9;
    }

    line(x1, y1, x2, y2);
  }
}

function mousePressed() {
  clickTime = millis();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
