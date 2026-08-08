```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Retro Snake</title>
<style>
    body {
        background-color: #000;
        color: #fff;
        font-family: 'Courier New', Courier, monospace;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100vh;
        margin: 0;
    }

    h1 {
        color: #0f0;
        text-shadow: 0 0 10px #0f0;
        margin-bottom: 10px;
    }

    #scoreBoard {
        font-size: 20px;
        margin-bottom: 10px;
    }

    canvas {
        border: 2px solid #0f0;
        box-shadow: 0 0 20px #0f0;
        background-color: #000;
    }

    #gameOverOverlay {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0, 0, 0, 0.85);
        display: none;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        z-index: 10;
    }

    #gameOverOverlay h2 {
        font-size: 40px;
        color: #f0f;
        text-shadow: 0 0 15px #f0f;
        margin-bottom: 20px;
    }

    #finalScore {
        font-size: 24px;
        margin-bottom: 30px;
    }

    button {
        background-color: #000;
        color: #fff;
        border: 2px solid #0f0;
        padding: 10px 20px;
        font-size: 18px;
        font-family: inherit;
        cursor: pointer;
        box-shadow: 0 0 10px #0f0;
        transition: all 0.2s;
    }

    button:hover {
        background-color: #0f0;
        color: #000;
        box-shadow: 0 0 20px #0f0;
    }

    .hidden {
        display: none !important;
    }
</style>
</head>
<body>

    <h1>RETRO SNAKE</h1>
    <div id="scoreBoard">Score: <span id="score">0</span></div>
    
    <canvas id="gameCanvas" width="400" height="400"></canvas>

    <div id="gameOverOverlay">
        <h2>GAME OVER</h2>
        <div id="finalScore">Score: 0</div>
        <button id="restartBtn">Restart</button>
    </div>

<script>
    const canvas = document.getElementById('gameCanvas');
    const ctx = canvas.getContext('2d');
    const scoreEl = document.getElementById('score');
    const finalScoreEl = document.getElementById('finalScore');
    const gameOverOverlay = document.getElementById('gameOverOverlay');
    const restartBtn = document.getElementById('restartBtn');

    const gridSize = 20;
    const tileCountX = canvas.width / gridSize;
    const tileCountY = canvas.height / gridSize;

    let score = 0;
    let gameRunning = false;
    let gameInterval;

    let snake = [];
    let food = { x: 0, y: 0 };
    
    let velocity = { x: 0, y: 0 };

    function initGame() {
        snake = [{ x: 10, y: 10 }, { x: 9, y: 10 }, { x: 8, y: 10 }];
        score = 0;
        velocity = { x: 1, y: 0 };
        placeFood();
        scoreEl.textContent = score;
        
        // Hide overlay and ensure no conflicts
        gameOverOverlay.style.display = 'none';
        
        if (gameInterval) clearInterval(gameInterval);
        gameRunning = true;
        gameInterval = setInterval(update, 100);
    }

    function placeFood() {
        food.x = Math.floor(Math.random() * tileCountX);
        food.y = Math.floor(Math.random() * tileCountY);
        
        // Ensure food doesn't spawn on snake
        for (let part of snake) {
            if (part.x === food.x && part.y === food.y) {
                placeFood();
                break;
            }
        }
    }

    function update() {
        if (!gameRunning) return;

        // Calculate new head position
        const head = { x: snake[0].x + velocity.x, y: snake[0].y + velocity.y };

        // Check collision with walls
        if (head.x < 0 || head.x >= tileCountX || head.y < 0 || head.y >= tileCountY) {
            endGame();
            return;
        }

        // Check collision with self
        for (let i = 0; i < snake.length; i++) {
            if (snake[i].x === head.x && snake[i].y === head.y) {
                endGame();
                return;
            }
        }

        snake.unshift(head); // Add new head

        // Check if food eaten
        if (head.x === food.x && head.y === food.y) {
            score++;
            scoreEl.textContent = score;
            placeFood();
        } else {
            snake.pop(); // Remove tail
        }

        draw();
    }

    function checkCollision(head) {
        if (head.x < 0 || head.x >= tileCountX || head.y < 0 || head.y >= tileCountY) {
            return true;
        }

        for (let i = 1; i < snake.length; i++) {
            if (snake[i].x === head.x && snake[i].y === head.y) {
                return true;
            }
        }

        return false;
    }

    function endGame() {
        gameRunning = false;
        clearInterval(gameInterval);
        finalScoreEl.textContent = "Score: " + score;
        gameOverOverlay.style.display = 'flex';
    }

    function draw() {
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        ctx.fillStyle = '#0f0';
        ctx.shadowBlur = 10;
        ctx.shadowColor = '#0f0';

        for (let part of snake) {
            ctx.fillRect(part.x * gridSize, part.y * gridSize, gridSize - 1, gridSize - 1);
        }

        ctx.fillStyle = '#f0f';
        ctx.shadowColor = '#f0f';
        ctx.shadowBlur = 15;

        ctx.fillRect(food.x * gridSize, food.y * gridSize, gridSize - 1, gridSize - 1);

        ctx.shadowBlur = 0;
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowUp' && velocity.y !== 1) {
            velocity = { x: 0, y: -1 };
        }
        else if (e.key === 'ArrowDown' && velocity.y !== -1) {
            velocity = { x: 0, y: 1 };
        }
        else if (e.key === 'ArrowLeft' && velocity.x !== 1) {
            velocity = { x: -1, y: 0 };
        }
        else if (e.key === 'ArrowRight' && velocity.x !== -1) {
            velocity = { x: 1, y: 0 };
        }
    });

    restartBtn.addEventListener('click', () => {
        initGame();
    });

    initGame();
    draw();
</script>
</body>
</html>
```