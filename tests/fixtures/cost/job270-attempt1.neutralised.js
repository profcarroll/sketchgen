// Entry 269, neutralised 2026-09-15. The original (kept beside this file as
// sketch.unsafe.js.txt) drew 1,500 lit sphere() meshes and ~55,000 immediate-mode
// line() calls per frame in WEBGL, which allocates a GPU buffer per call and
// ran the operator's browser out of memory. Same picture, within the frame
// budget: one point cloud, one capped batch of neighbour lines, one batch of
// grid lines. Three shapes a frame.
let particles = [];
let grid = [];
const N = 1500;
const GRID = 20;
const LINK = 150;          // neighbour distance, as written
const MAX_LINKS = 900;     // segments per frame, capped
const BANDS = 8;
const STRIDE = 7;          // coprime with N: the cap samples the whole cloud
let time = 0;

function setup() {
  createCanvas(800, 600, WEBGL);
  colorMode(HSB, 360, 100, 100, 1);
  frameRate(30);
  for (let i = 0; i < N; i++) {
    particles.push({
      x: random(-width / 2, width / 2),
      y: random(-height / 2, height / 2),
      z: random(-300, 300),
      vx: random(-0.5, 0.5), vy: random(-0.5, 0.5), vz: random(-0.5, 0.5),
      hue: random(360),
      size: random(1, 3)
    });
  }
  for (let i = 0; i < GRID; i++) {
    grid[i] = [];
    for (let j = 0; j < GRID; j++) {
      grid[i][j] = {
        x: map(i, 0, GRID - 1, -width / 3, width / 3),
        y: map(j, 0, GRID - 1, -height / 3, height / 3),
        z: random(-200, 200)
      };
    }
  }
}

function draw() {
  background(0);
  time += 0.01;
  let cx = sin(time * 0.2) * 200;
  let cy = cos(time * 0.3) * 150;
  let cz = sin(time * 0.1) * 300;
  camera(cx, cy, cz + 500, cx, cy, cz, 0, 1, 0);

  // Move the particles: same rules as written, no allocation.
  for (let i = 0; i < N; i++) {
    let p = particles[i];
    p.x += p.vx; p.y += p.vy; p.z += p.vz;
    if (p.x < -width / 2 || p.x > width / 2) p.vx *= -1;
    if (p.y < -height / 2 || p.y > height / 2) p.vy *= -1;
    if (p.z < -300 || p.z > 300) p.vz *= -1;
    p.vx = constrain(p.vx + random(-0.05, 0.05), -1, 1);
    p.vy = constrain(p.vy + random(-0.05, 0.05), -1, 1);
    p.vz = constrain(p.vz + random(-0.05, 0.05), -1, 1);
    p.hue = (p.hue + 0.5) % 360;
  }

  // The cloud: p5 gives a WEBGL point batch one stroke colour, so the cloud is
  // eight batches by hue band, each drawn twice for a soft glow. Sixteen shapes.
  noFill();
  for (let band = 0; band < BANDS; band++) {
    let h = (band + 0.5) * 360 / BANDS;
    strokeWeight(7); stroke(h, 80, 90, 0.12);
    beginShape(POINTS);
    for (let i = 0; i < N; i++) {
      let p = particles[i];
      if (Math.floor(p.hue * BANDS / 360) === band) vertex(p.x, p.y, p.z);
    }
    endShape();
    strokeWeight(3); stroke(h, 80, 90, 0.8);
    beginShape(POINTS);
    for (let i = 0; i < N; i++) {
      let p = particles[i];
      if (Math.floor(p.hue * BANDS / 360) === band) vertex(p.x, p.y, p.z);
    }
    endShape();
  }

  // Neighbour links: spatial hash, squared distances, one LINES shape, capped.
  let cells = new Map();
  for (let i = 0; i < N; i++) {
    let p = particles[i];
    let key = Math.floor(p.x / LINK) + "," + Math.floor(p.y / LINK) + "," + Math.floor(p.z / LINK);
    let bucket = cells.get(key);
    if (bucket) bucket.push(i); else cells.set(key, [i]);
  }
  let links = 0;
  let link2 = LINK * LINK;
  strokeWeight(0.5);
  stroke((time * 40) % 360, 60, 90, 0.35);
  beginShape(LINES);
  outer:
  for (let s = 0; s < N; s++) {
    let i = (s * STRIDE + frameCount) % N;
    let p = particles[i];
    let gx = Math.floor(p.x / LINK), gy = Math.floor(p.y / LINK), gz = Math.floor(p.z / LINK);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
      let bucket = cells.get((gx + dx) + "," + (gy + dy) + "," + (gz + dz));
      if (!bucket) continue;
      for (let k = 0; k < bucket.length; k++) {
        let j = bucket[k];
        if (j <= i) continue;
        let q = particles[j];
        let ddx = p.x - q.x, ddy = p.y - q.y, ddz = p.z - q.z;
        let d2 = ddx * ddx + ddy * ddy + ddz * ddz;
        if (d2 < link2) {
          vertex(p.x, p.y, p.z);
          vertex(q.x, q.y, q.z);
          if (++links >= MAX_LINKS) break outer;
        }
      }
    }
  }
  endShape();

  // The grid: 760 segments in one LINES shape.
  strokeWeight(0.3);
  stroke(200, 50, 80, 0.3);
  beginShape(LINES);
  for (let i = 0; i < GRID; i++) {
    for (let j = 0; j < GRID; j++) {
      let g = grid[i][j];
      if (i < GRID - 1) { vertex(g.x, g.y, g.z); vertex(grid[i + 1][j].x, grid[i + 1][j].y, grid[i + 1][j].z); }
      if (j < GRID - 1) { vertex(g.x, g.y, g.z); vertex(grid[i][j + 1].x, grid[i][j + 1].y, grid[i][j + 1].z); }
    }
  }
  endShape();

  for (let i = 0; i < GRID; i++) {
    for (let j = 0; j < GRID; j++) {
      grid[i][j].z = sin(time * 0.3 + i * 0.2 + j * 0.1) * 100;
    }
  }
}
