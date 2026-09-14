let letters = [];
let colors = [
  [255, 215, 0],    // Gold
  [0, 30, 60],      // Deep Blue
  [255, 255, 255],  // White
  [139, 0, 0],      // Dark Red
  [75, 0, 130]      // Indigo
];
let currentColorIndex = 0;
let time = 0;

function setup() {
  createCanvas(windowWidth, windowHeight);
  textSize(120);
  textAlign(CENTER, CENTER);
  
  letters = [
    'CINEMA',
    'OPENING',
    'SEQUENCE',
    'GRANDEUR',
    'THEATRICAL'
  ];
}

function draw() {
  background(0);
  
  time++;
  
  // Cycle through color palettes
  if (time % 300 === 0) {
    currentColorIndex = (currentColorIndex + 1) % colors.length;
  }
  
  const [r, g, b] = colors[currentColorIndex];
  
  for (let i = 0; i < letters.length; i++) {
    const letter = letters[i];
    const offset = i * 200;
    
    // Calculate position with sweeping motion
    const x = (width / 2) + sin(time / 50 + i) * width * 0.4;
    const y = (height / 2) + cos(time / 30 + i) * height * 0.3;
    
    // Scaling effect
    const scale = 0.8 + sin(time / 20 + i) * 0.3;
    
    // Fade in/out effect
    const alpha = 100 + sin(time / 15 + i) * 100;
    
    push();
    translate(x, y);
    scale(scale);
    
    fill(r, g, b, alpha);
    noStroke();
    text(letter, 0, 0);
    pop();
  }
  
  // Add dramatic pan effect
  if (time % 150 === 0) {
    // Change color palette every few seconds
    currentColorIndex = (currentColorIndex + 1) % colors.length;
  }
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
