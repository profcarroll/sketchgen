/* Six published entries, copied from the live gallery's kiosk.json on 2026-09-20 with
   each entry's sketch.js beside it, so the mockup can run the real sketches in-page.
   The real page frames e/<id>/sketch/ in a sandboxed iframe instead (spec §4). */
window.SWIPE_ENTRIES = [
 {
  "id": 987,
  "prompt": "a sweater being knit\n\nRevise: the same process of knitting, and this time the visible stitches should form a continuous, repeating geometric pattern across the growing fabric.\n\nRevise: the same continuously growing strip of fabric forming a sweater, and this time the visible stitches should consistently form distinct, alternating colored horizontal stripes.",
  "brief": "The canvas displays a textile material forming a growing strip of fabric, suggesting the process of knitting. As the fabric expands downwards, the surface is defined by distinct, visible stitches that maintain a uniform, repeating structure. The color pattern is cyclical, forming clear, alternating horizontal stripes of two or more distinct colors. The process of knitting itself provides the inherent movement, causing the patterned fabric to continuously grow and evolve across the viewing area, independent of user interaction.",
  "statement": "This sketch simulates the process of knitting by generating a growing strip of fabric on screen. Starting from the top, it builds rows of stitches that descend vertically, each row maintaining a consistent horizontal pattern with alternating colors. The animation proceeds automatically without any user input, showing the continuous evolution of the knitted material as new rows are added. The visual rhythm mimics traditional knitting patterns, where each stitch is clearly defined and contributes to a larger textured surface. The movement is smooth and steady, reflecting the mechanical repetition inherent in the craft, while the color cycling creates a visually pleasing striped effect that evolves over time.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 1,
  "generation": 2,
  "parent_entry_id": 943,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-19T05:56:16Z",
  "published_utc": "2026-09-19T14:19:10Z",
  "prompt_tokens": 1695,
  "completion_tokens": 540,
  "wall_s": 37.242,
  "licence": "CC BY 4.0",
  "canvas": [
   400,
   600
  ],
  "judgment": {},
  "href": "e/987/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/987/",
  "src": "let stitches = [];\nlet stripeColors = [];\nlet stripeHeight = 20;\nlet totalRows = 30;\nlet currentRow = 0;\n\nfunction setup() {\n  createCanvas(400, 600);\n  colorMode(HSB, 360, 100, 100, 1);\n  \n  // Define stripe colors\n  stripeColors = [\n    color(240, 80, 90), // Blue\n    color(0, 80, 90),   // Red\n    color(120, 80, 90)  // Green\n  ];\n  \n  // Initialize first row\n  stitches = [];\n  for (let i = 0; i < width; i += 10) {\n    stitches.push({ x: i, y: 0, colorIndex: floor(random(stripeColors.length)) });\n  }\n}\n\nfunction draw() {\n  background(220, 20, 95);\n  \n  // Draw existing stitches\n  for (let i = 0; i < stitches.length; i++) {\n    let s = stitches[i];\n    fill(s.colorIndex % stripeColors.length);\n    noStroke();\n    ellipse(s.x, s.y, 8, 8);\n  }\n  \n  // Add new row of stitches\n  if (currentRow < totalRows) {\n    let newRow = [];\n    for (let i = 0; i < width; i += 10) {\n      let x = i;\n      let y = currentRow * stripeHeight;\n      let colorIndex = floor(random(stripeColors.length));\n      newRow.push({ x: x, y: y, colorIndex: colorIndex });\n    }\n    stitches = stitches.concat(newRow);\n    currentRow++;\n  }\n  \n  // Animate the growth\n  if (currentRow >= totalRows) {\n    noLoop();\n  }\n}\n"
 },
 {
  "id": 827,
  "prompt": "the game of life\n\nRevise: the same cellular automaton, and this time increasing the grid size by a factor of two\n\nRevise: the same cellular automaton, and this time the background color darkens slightly over time\n\nRevise: the same cellular automaton, and this time some active cells flash brighter when they die\n\nRevise: the same simulation, and this time every newly born cell glows with a soft, residual light for one generation cycle.\n\nRevise: the same simulated biological process, and this time the background darkens in visible, rhythmic pulsing cycles instead of gradually.\n\nRevise: the same dynamically evolving field of interconnected cells, and this time the thin connection lines glow faintly when a cell is born.\n\nRevise: the same dynamically shifting lattice of interconnected, glowing cells, and this time expired cells dissipate into trails of residual light.\n\nRevise: the same constantly shifting lattice of interconnected, glowing cells, and this time the residual light trails that follow decay linger visibly for several full generation cycles.\n\nRevise: the same vast, dynamically evolving lattice of interconnected, glowing cells, and this time the background darkens and brightens in pronounced, slow-motion waves of light.",
  "brief": "The canvas displays a vast, dynamically evolving lattice composed of interconnected, glowing cells. The system is constantly shifting, with cells emerging and decaying in a natural cycle. When a cell dies, it does not instantly vanish; instead, it dissipates into delicate trails of residual light. These luminous echoes follow the cell's path, lingering visibly for multiple generation cycles, causing the entire field of glowing connections to subtly ebb and flow over time.",
  "statement": "This sketch creates a dynamic, evolving system of glowing cells that emerge and fade over time. Each cell has a lifecycle, gradually decaying until it dissipates into a luminous trail that persists briefly before disappearing. As cells die, they leave behind glowing echoes that form intricate networks of connections between nearby living cells. The entire field shifts constantly through random motion and natural birth/death cycles, creating an ever-changing lattice of light that pulses with organic rhythm. Viewers witness a subtle ebb and flow as the system balances new emergence against fading decay, producing a mesmerizing display of interconnected luminosity.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 1,
  "generation": 9,
  "parent_entry_id": 771,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-18T06:52:53Z",
  "published_utc": "2026-09-18T14:55:19Z",
  "prompt_tokens": 1513,
  "completion_tokens": 815,
  "wall_s": 46.061,
  "licence": "CC BY 4.0",
  "canvas": [
   800,
   600
  ],
  "judgment": {
   "agent": {
    "brief": {
     "n": 3,
     "pct": 0.832,
     "score": 2.573094816161215
    },
    "look": {
     "n": 3,
     "pct": 0.8826666666666667,
     "score": 3.8776614783751087
    }
   },
   "human": {
    "brief": {
     "n": 1,
     "pct": 0.4090909090909091,
     "score": 0.6412872478904895
    },
    "look": {
     "n": 1,
     "pct": 0.1322314049586777,
     "score": 0.3663776217506166
    }
   }
  },
  "href": "e/827/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/827/",
  "src": "let cells = [];\nlet trails = [];\nconst CELL_COUNT = 200;\nconst TRAIL_LENGTH = 30;\n\nclass Cell {\n  constructor(x, y) {\n    this.x = x;\n    this.y = y;\n    this.life = 1.0;\n    this.decay = random(0.001, 0.005);\n    this.size = random(5, 15);\n    this.color = color(random(100, 255), random(100, 255), 255, 200);\n    this.trail = [];\n  }\n\n  update() {\n    if (this.life > 0) {\n      this.life -= this.decay;\n    } else {\n      // Add to trails when dead\n      if (this.trail.length < TRAIL_LENGTH) {\n        this.trail.push({x: this.x, y: this.y});\n      }\n    }\n\n    // Random movement\n    this.x += random(-0.5, 0.5);\n    this.y += random(-0.5, 0.5);\n\n    // Keep within bounds\n    this.x = constrain(this.x, 0, width);\n    this.y = constrain(this.y, 0, height);\n  }\n\n  display() {\n    if (this.life > 0) {\n      fill(this.color);\n      noStroke();\n      ellipse(this.x, this.y, this.size * this.life);\n    } else {\n      // Draw trail\n      for (let i = 0; i < this.trail.length; i++) {\n        const t = this.trail[i];\n        const alpha = map(i, 0, this.trail.length, 0, 100);\n        fill(red(this.color), green(this.color), blue(this.color), alpha);\n        noStroke();\n        ellipse(t.x, t.y, this.size * (i / this.trail.length));\n      }\n    }\n  }\n}\n\nfunction setup() {\n  createCanvas(800, 600);\n  for (let i = 0; i < CELL_COUNT; i++) {\n    cells.push(new Cell(random(width), random(height)));\n  }\n}\n\nfunction draw() {\n  background(10, 10, 20);\n\n  // Update and display cells\n  for (let cell of cells) {\n    cell.update();\n    cell.display();\n\n    // Connect to nearby cells\n    for (let other of cells) {\n      if (cell !== other) {\n        const d = dist(cell.x, cell.y, other.x, other.y);\n        if (d < 100) {\n          stroke(255, 100);\n          strokeWeight(map(d, 0, 100, 0.5, 0));\n          line(cell.x, cell.y, other.x, other.y);\n        }\n      }\n    }\n\n    // Occasionally create new cell\n    if (random() < 0.001) {\n      cells.push(new Cell(random(width), random(height)));\n    }\n\n    // Remove dead cells that have decayed\n    if (cell.life <= 0 && cell.trail.length >= TRAIL_LENGTH) {\n      cells.splice(cells.indexOf(cell), 1);\n    }\n  }\n}\n"
 },
 {
  "id": 835,
  "prompt": "an image that only be New York City\n\nRevise: the same abstract aerial view of a metropolitan cityscape, and this time the flowing light streaks will sometimes intersect to create momentary, sharper geometric forms.\n\nRevise: create a monochromatic version\n\nRevise: the same abstract, monochromatic aerial panorama of an urban cityscape, and this time the intersecting light streaks will form defined, repeating starburst patterns at every major intersection point.\n\nRevise: the same abstract, aerial view of a densely packed cityscape, and this time the intersecting light streaks will sometimes form elongated, curved trails instead of perfect starbursts.\n\nRevise: the same abstract, monochromatic aerial panorama of a densely packed metropolitan cityscape, and this time the repeating starburst formations at intersections will be elongated, wispy floral blooms instead of geometric points.\n\nRevise: the same abstract aerial panorama of a densely packed metropolitan cityscape, and this time the intersecting light streaks will form sharp, angular crystalline clusters instead of elongated and wispy floral blooms.",
  "brief": "The canvas displays a deep, monochromatic aerial view of a sprawling, densely packed metropolitan cityscape. Streaks of light traverse the scene, appearing like flowing river currents of energy. At every major crossing point, these intersecting lines pause and bloom, forming delicate, repetitive, elongated floral clusters. The entire panorama maintains a continuous, flowing sense of movement, transforming the rigid city grid into an ethereal, blooming tapestry.",
  "statement": "This sketch creates an ethereal cityscape where rigid urban lines transform into flowing rivers of light. Using a dynamic flow field, thousands of streaking lines move through the space, their directions constantly influenced by invisible forces that simulate energy currents. At key intersections, these lines pause and bloom into delicate floral clusters, forming a tapestry where the mechanical grid of the city becomes organic and alive. The monochromatic palette with subtle color shifts evokes depth and movement, while the continuous animation demonstrates how structured systems can become fluid and dynamic through algorithmic interaction.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 2,
  "generation": 6,
  "parent_entry_id": 781,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-18T07:49:21Z",
  "published_utc": "2026-09-18T14:52:44Z",
  "prompt_tokens": 3267,
  "completion_tokens": 2225,
  "wall_s": 127.135,
  "licence": "CC BY 4.0",
  "canvas": [
   800,
   600
  ],
  "judgment": {
   "agent": {
    "brief": {
     "n": 1,
     "pct": 0.7613333333333333,
     "score": 2.1303954347672773
    },
    "look": {
     "n": 1,
     "pct": 0.248,
     "score": 0.46939642456999453
    }
   },
   "human": {
    "brief": {
     "n": 1,
     "pct": 0.19008264462809918,
     "score": 0.46939642456999453
    },
    "look": {
     "n": 1,
     "pct": 0.19008264462809918,
     "score": 0.46939642456999453
    }
   }
  },
  "href": "e/835/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/835/",
  "src": "let lines = [];\nlet points = [];\nlet flowField = [];\n\nfunction setup() {\n  createCanvas(800, 600);\n  colorMode(HSB, 360, 100, 100, 1);\n  noFill();\n  strokeWeight(1);\n\n  // Create flow field\n  let resolution = 20;\n  let cols = width / resolution;\n  let rows = height / resolution;\n  flowField = new Array(cols * rows);\n\n  for (let i = 0; i < flowField.length; i++) {\n    flowField[i] = random(TWO_PI);\n  }\n\n  // Create initial lines\n  for (let i = 0; i < 200; i++) {\n    let x = random(width);\n    let y = random(height);\n    lines.push({\n      x: x,\n      y: y,\n      angle: flowField[Math.floor(y / resolution) * cols + Math.floor(x / resolution)],\n      length: random(5, 30),\n      hue: random(20, 40),\n      sat: random(70, 90),\n      bri: random(80, 100),\n      alpha: random(0.3, 0.7)\n    });\n  }\n\n  // Create flower points\n  for (let i = 0; i < 150; i++) {\n    let x = random(width);\n    let y = random(height);\n    points.push({\n      x: x,\n      y: y,\n      size: random(2, 8),\n      hue: random(20, 40),\n      sat: random(70, 90),\n      bri: random(80, 100),\n      alpha: random(0.5, 1),\n      angle: random(TWO_PI)\n    });\n  }\n}\n\nfunction draw() {\n  background(0, 0, 5);\n\n  // Update flow field\n  let resolution = 20;\n  let cols = width / resolution;\n  let rows = height / resolution;\n\n  for (let i = 0; i < flowField.length; i++) {\n    let x = (i % cols) * resolution;\n    let y = Math.floor(i / cols) * resolution;\n    let angle = flowField[i];\n    let force = p5.Vector.fromAngle(angle);\n    force.mult(0.01);\n    stroke((angle * 180 / PI + frameCount * 0.5) % 360, 70, 90, 0.2);\n    line(x, y, x + force.x * 10, y + force.y * 10);\n  }\n\n  // Draw lines\n  for (let i = 0; i < lines.length; i++) {\n    let l = lines[i];\n    let x = l.x;\n    let y = l.y;\n    let angle = l.angle;\n\n    stroke(l.hue, l.sat, l.bri, l.alpha);\n    push();\n    translate(x, y);\n    rotate(angle);\n    line(0, 0, l.length, 0);\n    pop();\n\n    // Update position\n    let force = p5.Vector.fromAngle(angle);\n    l.x += force.x * 0.5;\n    l.y += force.y * 0.5;\n\n    // Reset if out of bounds\n    if (l.x < -10 || l.x > width + 10 || l.y < -10 || l.y > height + 10) {\n      l.x = random(width);\n      l.y = random(height);\n      let col = Math.floor(l.x / resolution);\n      let row = Math.floor(l.y / resolution);\n      if (col >= 0 && col < cols && row >= 0 && row < rows) {\n        l.angle = flowField[row * cols + col];\n      }\n    }\n\n    // Occasionally change angle\n    if (random() < 0.01) {\n      let col = Math.floor(l.x / resolution);\n      let row = Math.floor(l.y / resolution);\n      if (col >= 0 && col < cols && row >= 0 && row < rows) {\n        l.angle = flowField[row * cols + col];\n      }\n    }\n\n    // Bloom at intersections\n    if (random() < 0.02) {\n      let size = random(1, 3);\n      stroke(l.hue, l.sat, l.bri, l.alpha);\n      for (let j = 0; j < 8; j++) {\n        push();\n        rotate(TWO_PI * j / 8);\n        line(0, 0, size, 0);\n        pop();\n      }\n    }\n  }\n\n  // Draw points\n  for (let i = 0; i < points.length; i++) {\n    let p = points[i];\n    let x = p.x;\n    let y = p.y;\n\n    stroke(p.hue, p.sat, p.bri, p.alpha);\n    push();\n    translate(x, y);\n    rotate(p.angle);\n    ellipse(0, 0, p.size, p.size * 2);\n    pop();\n\n    // Occasionally bloom\n    if (random() < 0.01) {\n      stroke((p.hue + 180) % 360, p.sat, p.bri, p.alpha * 0.5);\n      for (let j = 0; j < 12; j++) {\n        push();\n        rotate(TWO_PI * j / 12);\n        line(0, 0, p.size, 0);\n        pop();\n      }\n    }\n\n    // Update angle\n    p.angle += 0.05;\n  }\n}\n"
 },
 {
  "id": 838,
  "prompt": "radar images\n\nRevise: the same pulsating field of concentric arcs, and this time the ring colors must cycle smoothly through the spectrum during each pulsation phase.\n\nRevise: the same dynamic, self-contained pattern of concentric arcs, and this time the outermost arcs must pulse with greater amplitude and a more visible delay.\n\nRevise: the same radial array of nested, glowing arcs, and this time the concentric arcs must pulsate with a visible, gradual widening gap between the pulses.\n\nRevise: the same continuous, self-contained radial array of glowing, nested arcs, and this time the color field must transition through the visible spectrum using highly defined, distinct bands of color.\n\nRevise: the same continuous, self-contained radial array of glowing, nested arcs, and this time the pulsing rhythm must be interrupted by a noticeable, rhythmic gap between every cycle.",
  "brief": "The canvas displays a pulsating, self-contained field composed of multiple concentric arcs. These glowing rings pulsate rhythmically, expanding and contracting in a consistent pattern. As the arcs pulse, their color dynamically cycles through the full visible spectrum, shifting smoothly from one hue to the next during each pulsation phase. The entire field exists in autonomous motion, shifting and glowing continuously without any required input from the viewer.",
  "statement": "This sketch creates a mesmerizing, self-contained field of pulsating concentric arcs that rhythmically expand and contract in unison. Each ring pulses with a slightly different timing, creating a harmonious wave-like motion across the canvas. The colors smoothly cycle through the entire visible spectrum, shifting from one hue to the next as the rings pulse, producing a continuous, hypnotic visual effect. The entire system operates autonomously without any external input, demonstrating a simple yet elegant form of generative art that evolves continuously over time.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 1,
  "generation": 5,
  "parent_entry_id": 784,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-18T08:04:12Z",
  "published_utc": "2026-09-18T14:49:45Z",
  "prompt_tokens": 1506,
  "completion_tokens": 455,
  "wall_s": 29.764,
  "licence": "CC BY 4.0",
  "canvas": [
   400,
   400
  ],
  "judgment": {
   "agent": {
    "brief": {
     "n": 2,
     "pct": 0.9133333333333333,
     "score": 3.6307810725122045
    },
    "look": {
     "n": 2,
     "pct": 0.4146666666666667,
     "score": 0.7648614512421149
    }
   },
   "human": {
    "brief": {
     "n": 1,
     "pct": 0.731404958677686,
     "score": 2.1303954347672773
    },
    "look": {
     "n": 1,
     "pct": 0.6942148760330579,
     "score": 2.1303954347672773
    }
   }
  },
  "href": "e/838/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/838/",
  "src": "let rings = [];\nlet hueOffset = 0;\n\nfunction setup() {\n  createCanvas(400, 400);\n  colorMode(HSB, 360, 100, 100, 1);\n  \n  // Create concentric arcs\n  for (let i = 0; i < 15; i++) {\n    rings.push({\n      radius: 20 + i * 20,\n      strokeWidth: 2 + i * 0.3,\n      speed: 0.01 + i * 0.002\n    });\n  }\n}\n\nfunction draw() {\n  background(0, 0, 0, 1);\n  \n  // Update hue offset for color cycling\n  hueOffset = (hueOffset + 0.5) % 360;\n  \n  // Draw each ring\n  for (let i = 0; i < rings.length; i++) {\n    let ring = rings[i];\n    \n    // Calculate pulsation\n    let pulse = sin(frameCount * ring.speed) * 0.3 + 1;\n    let radius = ring.radius * pulse;\n    \n    // Calculate color based on hue offset and ring index\n    let hue = (hueOffset + i * 24) % 360;\n    stroke(hue, 100, 100, 0.7);\n    noFill();\n    strokeWeight(ring.strokeWidth);\n    \n    // Draw arc\n    arc(width/2, height/2, radius*2, radius*2, 0, PI);\n  }\n}\n"
 },
 {
  "id": 702,
  "prompt": "animate your interpretation of the song \"A Love Supreme\" by John Coltrane\n\nRevise: try again without circles\n\nRevise: the same dynamic, angular geometric lines that radiate outward from a central point, and this time the overall pattern should adopt continuous, interlocking spiral formations.\n\nRevise: the same angular, dynamic geometric lines radiating outward from a central core, and this time the overall pattern should periodically contract inward before resuming its continuous spiral expansion.",
  "brief": "The canvas displays a continuous, dynamic field of angular geometric lines. These lines originate from a central point, radiating outward to form complex, interlocking spiral formations. The colors shift and evolve as the music plays, with the entire pattern periodically drawing inward toward the core before aggressively expanding once more, mimicking the energy of the piece. The geometry maintains a highly dynamic tension, moving constantly in time with the rhythm and intensity of the underlying sound, creating a visual representation of continuous, spiraling energy.",
  "statement": "This sketch generates a dynamic field of angular geometric lines that radiate from the center of the canvas, forming complex spiral patterns. The lines continuously evolve in shape and color as time progresses, with each frame creating new visual tension through movement and rhythm. The central spiral formation pulses inward and outward in sync with an internal tempo, mimicking the energy of music through pure geometric motion. The color palette shifts smoothly across the hue spectrum, creating a hypnotic effect that keeps the viewer engaged as patterns continuously reconfigure themselves. The entire composition maintains a sense of controlled chaos\u2014each element contributes to a larger, ever-changing whole without any single part dominating the visual field.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 1,
  "generation": 3,
  "parent_entry_id": 624,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-17T16:44:01Z",
  "published_utc": "2026-09-17T21:20:31Z",
  "prompt_tokens": 1556,
  "completion_tokens": 687,
  "wall_s": 80.336,
  "licence": "CC BY 4.0",
  "canvas": [
   600,
   600
  ],
  "judgment": {
   "agent": {
    "brief": {
     "n": 1,
     "pct": 0.43333333333333335,
     "score": 0.9315620125727438
    },
    "look": {
     "n": 1,
     "pct": 0.18666666666666668,
     "score": 0.38111923829106725
    }
   },
   "human": {
    "brief": {
     "n": 1,
     "pct": 0.7479338842975206,
     "score": 2.1303954347672773
    },
    "look": {
     "n": 1,
     "pct": 0.7107438016528925,
     "score": 2.1303954347672773
    }
   }
  },
  "href": "e/702/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/702/",
  "src": "let lines = [];\nlet time = 0;\nlet spiralAngle = 0;\n\nfunction setup() {\n  createCanvas(600, 600);\n  colorMode(HSB, 360, 100, 100, 1);\n  noFill();\n  strokeWeight(1);\n}\n\nfunction draw() {\n  background(0, 0, 0, 0.05);\n  \n  time += 0.02;\n  spiralAngle += 0.01;\n\n  const centerX = width / 2;\n  const centerY = height / 2;\n  const maxRadius = min(width, height) * 0.45;\n  const numLines = 150;\n  \n  for (let i = 0; i < numLines; i++) {\n    const angle = (i / numLines) * TWO_PI * 3 + time + spiralAngle;\n    const radius = map(sin(time * 2 + i * 0.1), -1, 1, 0, maxRadius);\n    \n    const x1 = centerX + cos(angle) * radius;\n    const y1 = centerY + sin(angle) * radius;\n    const x2 = centerX + cos(angle + PI/4) * (radius * 1.5);\n    const y2 = centerY + sin(angle + PI/4) * (radius * 1.5);\n\n    const hue = (i * 2 + time * 30) % 360;\n    stroke(hue, 80, 90, 0.7);\n    \n    line(x1, y1, x2, y2);\n  }\n  \n  // Draw central spiral pattern\n  push();\n  translate(centerX, centerY);\n  rotate(time * 0.5);\n  for (let i = 0; i < 100; i++) {\n    const angle = i * 0.3 + time;\n    const radius = i * 2;\n    const x1 = cos(angle) * radius;\n    const y1 = sin(angle) * radius;\n    const x2 = cos(angle + 0.5) * (radius + 5);\n    const y2 = sin(angle + 0.5) * (radius + 5);\n    \n    const hue = (angle * 30 + time * 50) % 360;\n    stroke(hue, 80, 90, 0.5);\n    line(x1, y1, x2, y2);\n  }\n  pop();\n}\n"
 },
 {
  "id": 128,
  "prompt": "A grid of small squares, each breathing at its own slow rhythm, on a near-black ground with one warm hue. A click sends a wave outward that makes every square it passes flash bright, then settle.\n\nRevise: the same grid, and this time the pulse colors are randomized upon impact\n\nRevise: the same grid of small squares, and this time the squares' breathing rhythm subtly shifts across different quadrants\n\nRevise: the same uniform grid composed of small, interconnected squares, and this time the expanding wave pulse flashes a randomized spectrum of colors",
  "brief": "A continuous grid of small, interconnected squares rests on a near-black background, each square exhibiting its own slow, subtle breathing rhythm. Interacting with the canvas by clicking initiates an expanding wave that radiates outward from the point of contact. As this wave passes over the stationary squares, each square within its path flashes a burst of highly randomized, vibrant colors before returning to its steady, rhythmic pulse.",
  "statement": "This sketch creates a mesmerizing grid of squares that pulse gently with their own breathing rhythm, set against a near-black background. Each square has its own subtle animation cycle, creating a sense of organic life across the canvas. When clicked, an expanding wave radiates outward from the center, causing affected squares to flash vivid, randomized colors before returning to their steady rhythm. The interaction and continuous motion make for a calming yet responsive visual experience.",
  "submitted_by": "profcarroll",
  "planner": "gemma4:e4b",
  "executor": "qwen3-coder:30b-a3b-q4_K_M",
  "rules_file": "control",
  "attempts": 1,
  "generation": 3,
  "parent_entry_id": 86,
  "critique_by": "gemma4:e4b",
  "created_utc": "2026-09-14T21:21:15Z",
  "published_utc": "2026-09-14T22:42:56Z",
  "prompt_tokens": 1193,
  "completion_tokens": 792,
  "wall_s": 76.589,
  "licence": "CC BY 4.0",
  "canvas": [
   600,
   400
  ],
  "judgment": {
   "agent": {
    "brief": {
     "n": 9,
     "pct": 0.5906666666666667,
     "score": 1.3457650080422903
    },
    "look": {
     "n": 9,
     "pct": 0.15066666666666667,
     "score": 0.3150345993019552
    }
   },
   "human": {
    "brief": {
     "n": 1,
     "pct": 0.28512396694214875,
     "score": 0.46939642456999453
    },
    "look": {
     "n": 1,
     "pct": 0.2975206611570248,
     "score": 0.46939642456999453
    }
   }
  },
  "href": "e/128/",
  "url": "https://profcarroll.github.io/sketchgen-gallery/e/128/",
  "src": "let grid = [];\nlet squareSize = 20;\nlet gridSizeX = 30;\nlet gridSizeY = 20;\nlet waveRadius = 0;\nlet waveSpeed = 1;\nlet waveCenter = { x: 0, y: 0 };\nlet isWaveActive = false;\n\nfunction setup() {\n  createCanvas(600, 400);\n  colorMode(HSB, 360, 100, 100, 1);\n  noStroke();\n  \n  for (let y = 0; y < gridSizeY; y++) {\n    grid[y] = [];\n    for (let x = 0; x < gridSizeX; x++) {\n      grid[y][x] = {\n        hue: random(360),\n        saturation: 80 + random(20),\n        brightness: 50 + random(30),\n        pulseSpeed: random(0.01, 0.03),\n        pulseOffset: random(TWO_PI),\n        flashColor: null,\n        isFlashing: false\n      };\n    }\n  }\n}\n\nfunction draw() {\n  background(10);\n  \n  for (let y = 0; y < gridSizeY; y++) {\n    for (let x = 0; x < gridSizeX; x++) {\n      let square = grid[y][x];\n      \n      // Calculate pulse value\n      let pulseValue = sin(frameCount * square.pulseSpeed + square.pulseOffset);\n      let brightness = square.brightness + pulseValue * 20;\n      \n      // Handle flashing\n      if (square.isFlashing) {\n        fill(square.flashColor);\n        square.isFlashing = false; // Reset flash after one frame\n      } else {\n        fill(square.hue, square.saturation, brightness);\n      }\n      \n      rect(x * squareSize, y * squareSize, squareSize, squareSize);\n    }\n  }\n  \n  // Draw wave if active\n  if (isWaveActive) {\n    stroke(255, 0.7);\n    noFill();\n    ellipse(waveCenter.x, waveCenter.y, waveRadius * 2);\n    waveRadius += waveSpeed;\n    \n    // Check if wave has passed all squares\n    let maxDist = dist(0, 0, width, height);\n    if (waveRadius > maxDist) {\n      isWaveActive = false;\n    }\n  }\n}\n\nfunction mousePressed() {\n  waveCenter = { x: mouseX, y: mouseY };\n  isWaveActive = true;\n  waveRadius = 0;\n  \n  // Trigger flash for squares within wave range\n  let centerX = Math.floor(mouseX / squareSize);\n  let centerY = Math.floor(mouseY / squareSize);\n  \n  for (let y = 0; y < gridSizeY; y++) {\n    for (let x = 0; x < gridSizeX; x++) {\n      let distToCenter = dist(x * squareSize, y * squareSize, mouseX, mouseY);\n      \n      if (distToCenter <= waveRadius + squareSize) {\n        grid[y][x].isFlashing = true;\n        grid[y][x].flashColor = color(random(360), 100, 100);\n      }\n    }\n  }\n}\n"
 }
];
