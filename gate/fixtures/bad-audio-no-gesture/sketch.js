// Verbatim from the 2026-09-13 session (dossier 7): audioInput.start() in setup() has no user gesture, so the AudioContext stays suspended and it never throws.
// p5.js Skills Showcase
// Demonstrating multiple programming and creative skills

let particles = [];
let connections = [];
let forceFields = [];
let audioInput;
let fft;
let bgColor;

function setup() {
  createCanvas(windowWidth, windowHeight);
  
  // Create initial particles
  for (let i = 0; i < 50; i++) {
    particles.push(new Particle(random(width), random(height)));
  }
  
  // Setup audio input
  audioInput = new p5.AudioIn();
  audioInput.start();
  
  // Setup FFT analyser
  fft = new p5.FFT(0.8, 1024);
  fft.setInput(audioInput);
  
  // Set initial background color
  bgColor = color(10, 15, 30);
}

function draw() {
  // Create a semi-transparent overlay for trail effect
  background(bgColor, 30);
  
  // Analyze audio input
  let spectrum = fft.analyze();
  let bass = fft.getEnergy("bass");
  let mid = fft.getEnergy("mid");
  let treble = fft.getEnergy("treble");
  
  // Map audio to visual parameters
  let audioHue = map(bass, 0, 255, 0, 360);
  let audioSaturation = map(mid, 0, 255, 50, 100);
  let audioBrightness = map(treble, 0, 255, 50, 100);
  
  // Update background color based on audio
  bgColor = color(audioHue, audioSaturation, audioBrightness);
  
  // Update and display particles
  for (let i = particles.length - 1; i >= 0; i--) {
    let p = particles[i];
    
    // Apply forces from force fields
    for (let field of forceFields) {
      p.applyForce(field.forceAt(p.position));
    }
    
    // Update particle
    p.update();
    
    // Display particle
    p.display();
    
    // Remove if off screen
    if (p.isOffScreen()) {
      particles.splice(i, 1);
    }
  }
  
  // Connect nearby particles
  connections = [];
  for (let i = 0; i < particles.length; i++) {
    for (let j = i + 1; j < particles.length; j++) {
      let d = dist(
        particles[i].position.x, 
        particles[i].position.y,
        particles[j].position.x, 
        particles[j].position.y
      );
      
      if (d < 100) {
        connections.push({
          p1: particles[i],
          p2: particles[j],
          distance: d
        });
      }
    }
  }
  
  // Display connections
  for (let conn of connections) {
    let alpha = map(conn.distance, 0, 100, 255, 0);
    stroke(255, alpha * 0.3);
    line(
      conn.p1.position.x,
      conn.p1.position.y,
      conn.p2.position.x,
      conn.p2.position.y
    );
  }
  
  // Add new particles occasionally
  if (frameCount % 30 === 0 && particles.length < 150) {
    particles.push(new Particle(random(width), random(height)));
  }
}

function mousePressed() {
  // Add particle at mouse position
  particles.push(new Particle(mouseX, mouseY));
  
  // Add force field at mouse position
  forceFields.push(new ForceField(mouseX, mouseY, 100, 1));
}

function mouseDragged() {
  // Add continuous force fields while dragging
  if (mouseX > 0 && mouseX < width && mouseY > 0 && mouseY < height) {
    forceFields.push(new ForceField(mouseX, mouseY, 50, 0.5));
  }
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}

class Particle {
  constructor(x, y) {
    this.position = createVector(x, y);
    this.velocity = p5.Vector.random2D();
    this.velocity.mult(random(0.5, 2));
    this.acceleration = createVector(0, 0);
    this.size = random(2, 6);
    this.hue = random(360);
    this.saturation = random(70, 100);
    this.brightness = random(70, 100);
    this.life = 255;
  }
  
  applyForce(force) {
    this.acceleration.add(force);
  }
  
  update() {
    this.velocity.add(this.acceleration);
    this.position.add(this.velocity);
    this.acceleration.mult(0);
    
    // Apply damping
    this.velocity.mult(0.95);
    
    // Slowly fade out
    this.life -= 1;
    
    // Boundary check with bounce
    if (this.position.x < 0 || this.position.x > width) {
      this.velocity.x *= -1;
      this.position.x = constrain(this.position.x, 0, width);
    }
    if (this.position.y < 0 || this.position.y > height) {
      this.velocity.y *= -1;
      this.position.y = constrain(this.position.y, 0, height);
    }
  }
  
  display() {
    noStroke();
    fill(this.hue, this.saturation, this.brightness, this.life);
    
    // Glow effect
    drawingContext.shadowBlur = 15;
    drawingContext.shadowColor = color(this.hue, this.saturation, this.brightness);
    
    ellipse(this.position.x, this.position.y, this.size * 2);
    
    // Reset shadow
    drawingContext.shadowBlur = 0;
  }
  
  isOffScreen() {
    return (
      this.position.x < -this.size || 
      this.position.x > width + this.size ||
      this.position.y < -this.size || 
      this.position.y > height + this.size ||
      this.life <= 0
    );
  }
}

class ForceField {
  constructor(x, y, radius, strength) {
    this.position = createVector(x, y);
    this.radius = radius;
    this.strength = strength;
  }
  
  forceAt(point) {
    let distance = p5.Vector.dist(this.position, point);
    
    if (distance < this.radius) {
      let force = p5.Vector.sub(this.position, point);
      force.normalize();
      force.mult(this.strength);
      
      // Ease-out the force based on distance
      let ease = map(distance, 0, this.radius, 1, 0);
      force.mult(ease);
      
      return force;
    }
    
    return createVector(0, 0);
  }
}
