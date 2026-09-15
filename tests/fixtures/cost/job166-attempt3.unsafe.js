let particles = [];
let connections = [];
let time = 0;

function setup() {
  createCanvas(windowWidth, windowHeight, WEBGL);
  colorMode(HSB, 360, 100, 100, 1);
  
  // Create thousands of particles
  for (let i = 0; i < 2000; i++) {
    particles.push({
      pos: p5.Vector.random3D().mult(random(200, 800)),
      vel: p5.Vector.random3D().mult(random(0.1, 0.5)),
      size: random(1, 4),
      hue: random(180, 240)
    });
  }
}

function draw() {
  background(0);
  time += 0.01;
  
  // Camera movement
  let cx = sin(time * 0.2) * 300;
  let cy = cos(time * 0.3) * 300;
  let cz = sin(time * 0.1) * 300;
  camera(0, 0, -500 + cz, cx, cy, 0, 0, 1, 0);
  
  // Draw particles
  for (let i = 0; i < particles.length; i++) {
    let p = particles[i];
    
    // Update position with some noise
    p.pos.add(p.vel);
    
    // Wrap around the world
    if (p.pos.mag() > 1000) {
      p.pos = p5.Vector.random3D().mult(900);
    }
    
    // Draw particle
    push();
    translate(p.pos.x, p.pos.y, p.pos.z);
    noStroke();
    fill(p.hue, 80, 90, 0.8);
    sphere(p.size);
    pop();
    
    // Draw connections to nearby particles
    for (let j = i + 1; j < particles.length; j++) {
      let other = particles[j];
      let d = p.pos.dist(other.pos);
      
      if (d < 150) {
        stroke(p.hue, 80, 90, 0.2);
        line(p.pos.x, p.pos.y, p.pos.z, other.pos.x, other.pos.y, other.pos.z);
      }
    }
  }
  
  // Add some dynamic tendrils
  push();
  noFill();
  stroke(180, 80, 90, 0.5);
  beginShape();
  for (let i = 0; i < 100; i++) {
    let angle = time + i * 0.2;
    let radius = 300 + sin(time * 2 + i * 0.1) * 100;
    let x = radius * cos(angle);
    let y = radius * sin(angle);
    let z = sin(time + i * 0.1) * 200;
    vertex(x, y, z);
  }
  endShape();
  pop();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
