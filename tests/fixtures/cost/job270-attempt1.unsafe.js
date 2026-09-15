let particles = [];
let connections = [];
let gridSize = 20;
let grid = [];
let time = 0;

function setup() {
  createCanvas(800, 600, WEBGL);
  colorMode(HSB, 360, 100, 100, 1);

  // Initialize grid
  for (let i = 0; i < gridSize; i++) {
    grid[i] = [];
    for (let j = 0; j < gridSize; j++) {
      grid[i][j] = { x: 0, y: 0, z: 0 };
    }
  }

  // Create particles
  for (let i = 0; i < 1500; i++) {
    particles.push({
      x: random(-width/2, width/2),
      y: random(-height/2, height/2),
      z: random(-300, 300),
      vx: random(-0.5, 0.5),
      vy: random(-0.5, 0.5),
      vz: random(-0.5, 0.5),
      hue: random(360),
      size: random(1, 3)
    });
  }

  // Initialize grid positions
  for (let i = 0; i < gridSize; i++) {
    for (let j = 0; j < gridSize; j++) {
      grid[i][j].x = map(i, 0, gridSize-1, -width/3, width/3);
      grid[i][j].y = map(j, 0, gridSize-1, -height/3, height/3);
      grid[i][j].z = random(-200, 200);
    }
  }

  // Create initial connections
  for (let i = 0; i < particles.length; i++) {
    for (let j = i + 1; j < particles.length; j++) {
      let d = dist(particles[i].x, particles[i].y, particles[i].z,
                   particles[j].x, particles[j].y, particles[j].z);
      if (d < 150) {
        connections.push({
          p1: i,
          p2: j,
          alpha: map(d, 0, 150, 0.8, 0)
        });
      }
    }
  }
}

function draw() {
  background(0);
  time += 0.01;

  // Camera movement
  let cx = sin(time * 0.2) * 200;
  let cy = cos(time * 0.3) * 150;
  let cz = sin(time * 0.1) * 300;
  camera(cx, cy, cz + 500, cx, cy, cz, 0, 1, 0);

  // Update and draw particles
  for (let i = 0; i < particles.length; i++) {
    let p = particles[i];
    
    // Apply forces
    p.x += p.vx;
    p.y += p.vy;
    p.z += p.vz;
    
    // Boundary check
    if (p.x < -width/2 || p.x > width/2) p.vx *= -1;
    if (p.y < -height/2 || p.y > height/2) p.vy *= -1;
    if (p.z < -300 || p.z > 300) p.vz *= -1;
    
    // Add some random movement
    p.vx += random(-0.05, 0.05);
    p.vy += random(-0.05, 0.05);
    p.vz += random(-0.05, 0.05);
    
    // Clamp velocity
    p.vx = constrain(p.vx, -1, 1);
    p.vy = constrain(p.vy, -1, 1);
    p.vz = constrain(p.vz, -1, 1);
    
    // Update hue for color shifting
    p.hue = (p.hue + 0.5) % 360;
    
    // Draw particle
    push();
    translate(p.x, p.y, p.z);
    noStroke();
    fill(p.hue, 80, 90, 0.8);
    sphere(p.size);
    pop();
  }

  // Draw connections
  strokeWeight(0.5);
  for (let i = 0; i < connections.length; i++) {
    let c = connections[i];
    let p1 = particles[c.p1];
    let p2 = particles[c.p2];
    
    if (!p1 || !p2) continue;
    
    let d = dist(p1.x, p1.y, p1.z, p2.x, p2.y, p2.z);
    c.alpha = map(d, 0, 150, 0.8, 0);
    
    stroke(hue(p1.hue), 70, 90, c.alpha);
    line(p1.x, p1.y, p1.z, p2.x, p2.y, p2.z);
  }

  // Add some grid connections for structure
  strokeWeight(0.3);
  stroke(200, 50, 80, 0.3);
  for (let i = 0; i < gridSize; i++) {
    for (let j = 0; j < gridSize; j++) {
      if (i < gridSize - 1) {
        line(
          grid[i][j].x, grid[i][j].y, grid[i][j].z,
          grid[i+1][j].x, grid[i+1][j].y, grid[i+1][j].z
        );
      }
      if (j < gridSize - 1) {
        line(
          grid[i][j].x, grid[i][j].y, grid[i][j].z,
          grid[i][j+1].x, grid[i][j+1].y, grid[i][j+1].z
        );
      }
    }
  }

  // Update grid positions for animation
  for (let i = 0; i < gridSize; i++) {
    for (let j = 0; j < gridSize; j++) {
      let t = time * 0.3 + i * 0.2 + j * 0.1;
      grid[i][j].z = sin(t) * 100;
    }
  }

  // Occasionally stabilize connections
  if (frameCount % 100 === 0) {
    for (let i = 0; i < connections.length; i++) {
      let c = connections[i];
      if (random() < 0.3) {  // 30% chance to change connection
        c.alpha = random(0.2, 0.8);
      }
    }
  }
}
