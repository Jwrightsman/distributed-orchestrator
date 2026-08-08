```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Neon Snake</title>
<style>
  :root {
    --bg-color: #050505;
    --canvas-bg: #111;
    --neon-green: #39ff39;
    --neon-pink: #ff00ff;
    --text-color: #fff;
  }
  
  body {
    background-color: var(--bg-color);
    color: var(--text-color);
    font-family: 'Courier New', Courier, monospace;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    margin: 0;
    height: 100vh;
    overflow: hidden;
  }
  
  h1 {
    color: var(--neon-pink);
    text-shadow: 0 0 10px var(--neon-pink);
    margin-bottom: 10px;
    font-size: 2rem;
    letter-spacing: 2px;
  }
  
  #score {
    font-size: 1.5rem;
    margin-bottom: 15px;
    color: var(--neon-green);
    text-shadow: 0 0 5px var(--neon-green);
  }
  
  #gameCanvas {
    background-color: var(--canvas-bg);
    border: 2px solid #333;
    box-shadow: 0 0 20px rgba(57, 255, 57, 0.2);
  }
  
  #gameOverOverlay {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    display: none;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    background-color: rgba(0, 0, 0, 0.85);
    backdrop-filter: blur(2px);
    z-index: 10;
  }
  
  #gameOverOverlay h2 {
    font-size: 3rem;
    color: var(--neon-pink);
    text-shadow: 0 0 15px var(--neon-pink);
    margin-bottom: 30px;
  }
  
  #gameOverOverlay button {
    background: transparent;
    border: 2px solid var(--neon-green);
    color: var(--neon-green);
    padding: 15px 30px;
    font-size: 1.2rem;
    font-family: inherit;
    font-weight: bold;
    cursor: pointer;
    box-shadow: 0 0 10px var(--neon-green);
    transition: all 0.2s ease;
  }
  
  #gameOverOverlay button:hover {
    background-color: var(--neon-green);
    color: #000;
    box-shadow: 0 0 20px var(--neon-green);
  }
</style>
</head>
<body>
  <h1>NEON SNAKE</h1>
  <div id="score">0</div>
  <canvas id="gameCanvas" width="400" height="400"></canvas>
  
  <div id="gameOverOverlay">
    <h2>GAME OVER</h2>
    <button id="restartBtn">Restart</button>
  </div>
  
<script>
    const canvas = document.getElementById('gameCanvas');
    const ctx = canvas.getContext('2d');
    const scoreEl = document.getElementById('score');
    const restartBtn = document.getElementById('restartBtn');
    const gameOverOverlay = document.getElementById('gameOverOverlay');

    const gridSize = 20;
    const tileCountX = canvas.width / gridSize;
    const tileCountY = canvas.height / gridSize;

    let score = 0;
    let gameOver = false;
    let gameIntervalId = null;
    let snake = [];
    let food = { x: 0, y: 0 };
    let velocity = { x: 0, y: 0 };
    let gameRunning = true;

    // --- Core Logic Functions ---

    function initGame() {
        // Reset state
        snake = [
            { x: 10, y: 10 },
            { x: 9, y: 10 },
            { x: 8, y: 10 }
        ];
        velocity = { x: 1, y: 0 };
        score = 0;
        gameOver = false;
        gameRunning = true;
        placeFood();
        
        // UI Reset
        scoreEl.textContent = score;
        gameOverOverlay.style.display = 'none';

        // Start Loop
        if (gameIntervalId) clearInterval(gameIntervalId);
        gameIntervalId = setInterval(gameLoop, 150);
    }

    function gameLoop() {
        if (!gameRunning) return;
        update();
        draw();
    }

    function update() {
        // Move Snake
        const head = { 
            x: snake[0].x + velocity.x, 
            y: snake[0].y + velocity.y 
        };
        snake.unshift(head);

        // Collision Detection
        if (checkCollision(head)) {
            endGame();
            return;
        }

        // Eat Food
        if (head.x === food.x && head.y === food.y) {
            score++;
            scoreEl.textContent = score;
            placeFood();
        } else {
            snake.pop();
        }
    }

    function checkCollision(head) {
        // Wall Collision
        if (head.x < 0 || head.x >= tileCountX || head.y < 0 || head.y >= tileCountY) {
            return true;
        }
        // Self Collision
        for (let i = 1; i < snake.length; i++) {
            if (head.x === snake[i].x && head.y === snake[i].y) {
                return true;
            }
        }
        return false;
    }

    function placeFood() {
        let validPosition = false;
        while (!validPosition) {
            food.x = Math.floor(Math.random() * tileCountX);
            food.y = Math.floor(Math.random() * tileCountY);
            
            // Ensure food doesn't spawn on snake
            validPosition = true;
            for (let part of snake) {
                if (part.x === food.x && part.y === food.y) {
                    validPosition = false;
                    break;
                }
            }
        }
    }

    // --- Render Functions ---
    
    function draw() {
        // Clear Screen
        ctx.fillStyle = '#111';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        // Draw Snake
        ctx.fillStyle = '#39ff39'; // Neon Green
        for (let i = 0; i < snake.length; i++) {
            // Add glow effect
            ctx.shadowBlur = 10;
            ctx.shadowColor = '#39ff39';
            ctx.fillRect(snake[i].x * gridSize, snake[i].y * gridSize, gridSize - 2, gridSize - 2);
        }
        ctx.shadowBlur = 0; // Reset

        // Draw Food
        ctx.fillStyle = '#ff00ff'; // Neon Pink
        ctx.shadowBlur = 10;
        ctx.shadowColor = '#ff00ff';
        ctx.fillRect(food.x * gridSize, food.y * gridSize, gridSize - 2, gridSize - 2);
    }

    function endGame() {
        gameOver = true;
        gameRunning = false;
        gameOverOverlay.style.display = 'flex';
        clearInterval(gameIntervalId);
    }

    function restartGame() {
        initGame();
    }

    // --- Input Handling ---
    document.addEventListener('keydown', (e) => {
        switch(e.key) {
            case 'ArrowUp':
                if (velocity.y !== 1) velocity = { x: 0, y: -1 };
                break;
            case 'ArrowDown':
                if (velocity.y !== -1) velocity = { x: 0, y: 1 };
                break;
            case 'ArrowLeft':
                if (velocity.x !== 1) velocity = { x: -1, y: 0 };
                break;
            case 'ArrowRight':
                if (velocity.x !== -1) velocity = { x: 1, y: 0 };
                break;
        }
    });

    restartBtn.addEventListener('click', restartGame);

    // --- Initialization ---
    // Run immediately on load as per requirements (no start screen required)
    initGame();
</script>
</body>
</html>
```