let hexSize = 50;
let grid = [];
let time = 0;

function setup() {
  createCanvas(windowWidth, windowHeight);
  noStroke();
  
  // Initialize grid of hexagons
  for (let y = -hexSize; y < height + hexSize; y += hexSize * 1.8) {
    let row = [];
    for (let x = -hexSize; x < width + hexSize; x += hexSize * 2.5) {
      row.push({x, y});
    }
    grid.push(row);
  }
}

function draw() {
  background(10, 5, 20);
  
  time += 0.01;
  
  for (let i = 0; i < grid.length; i++) {
    for (let j = 0; j < grid[i].length; j++) {
      let hexX = grid[i][j].x + sin(time + i * 0.2) * 10;
      let hexY = grid[i][j].y + cos(time + j * 0.3) * 10;
      
      // Hexagon rotation and scaling
      let angle = time * 0.5 + i * 0.1 + j * 0.1;
      let scale = 0.8 + sin(time * 2 + i + j) * 0.2;
      
      push();
      translate(hexX, hexY);
      rotate(angle);
      scale(scale);
      
      // Color palette - vibrant 80s hues
      let hue = (time * 30 + i * 10 + j * 5) % 360;
      fill(hue, 90, 85, 0.7);
      
      // Draw hexagon
      drawHexagon(hexSize * 0.8);
      pop();
    }
  }
}

function drawHexagon(size) {
  beginShape();
  for (let i = 0; i < 6; i++) {
    let angle = TWO_PI / 6 * i;
    let x = cos(angle) * size;
    let y = sin(angle) * size;
    vertex(x, y);
  }
  endShape(CLOSE);
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
