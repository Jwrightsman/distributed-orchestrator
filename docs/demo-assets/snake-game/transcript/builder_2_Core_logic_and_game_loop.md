```html
<!DOCTYPE html>
<html>
<head>
    <title>Neon Snake</title>
    <style>
        body {
            background-color: #000;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            color: #fff;
            font-family: 'Courier New', Courier, monospace;
            overflow: hidden;
        }
        canvas {
            border: 2px solid #0ff;
            box-shadow: 0 0 20px #0ff;
            background-color: #000;
        }
        #gameOverOverlay {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.85);
            display: none;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            z-index: 10;
        }
        h1 {
            font-size: 48px;
            color: #f0f;
            text-shadow: 0 0 10px #f0f;
            margin-bottom: 20px;
        }
        p {
            font-size: 18px;
            margin-bottom: 30px;
        }
        button {
            background: transparent;
            color: #0f0;
            border: 2px solid #0f0;
            padding: 10px 20px;
            font-size: 18px;
            font-family: inherit;
            cursor: pointer;
            box-shadow: 0 0 10px #0f0;
            transition: all 0.2s;
        }
        button:hover {
            background: #0f0;
            color: #000;
        }
    </style>
</head>
<body>
    <canvas id="gameCanvas" width="400" height="400"></canvas>
    <div id="gameOverOverlay">
        <h1>GAME OVER</h1>
        <p>Score: <span id="finalScore">0</span></p>
        <button id="restartBtn">Restart Game</button>
    </div>
    <div id="score">Score: 0</div>

    <script>
        const canvas = document.getElementById('gameCanvas');
        const ctx = canvas.getContext('2d');
        const gameOverOverlay = document.getElementById('gameOverOverlay');
        const finalScoreEl = document.getElementById('finalScore');
        const restartBtn = document.getElementById('restartBtn');
        const scoreEl = document.getElementById('score');

        const gridSize = 20;
        const tileCountX = canvas.width / gridSize;
        const tileCountY = canvas.height / gridSize;

        let score = 0;
        let gameOver = false;
        let gameRunning = true;
        let snake = [];
        let food = { x: 0, y: 0 };
        let velocity = { x: 0, y: 0 };
        let gameInterval;

        function getRandomInt(min, max) {
            return Math.floor(Math.random() * (max - min)) + min;
        }

        function resetGame() {
            snake = [];
            food = { x: 0, y: 0 };
            score = 0;
            velocity = { x: 1, y: 0 };
            gameOver = false;
            gameRunning = true;
            gameOverOverlay.style.display = 'none';
            scoreEl.textContent = `Score: ${score}`;
            initGame();
        }

        function initGame() {
            snake = [
                { x: 10, y: 10 },
                { x: 9, y: 10 },
                { x: 8, y: 10 }
            ];
            
            placeFood();
            
            clearInterval(gameInterval);
            gameInterval = setInterval(update, 150);
        }

        function placeFood() {
            let validPosition = false;
            while (!validPosition) {
                food.x = getRandomInt(0, tileCountX - 1);
                food.y = getRandomInt(0, tileCountY - 1);
                validPosition = true;
                
                for (let part of snake) {
                    if (part.x === food.x && part.y === food.y) {
                        validPosition = false;
                        break;
                    }
                }
            }
        }

        function update() {
            if (!gameRunning) return;

            const head = { x: snake[0].x + velocity.x, y: snake[0].y + velocity.y };
            snake.unshift(head);

            if (head.x === food.x && head.y === food.y) {
                score += 10;
                scoreEl.textContent = `Score: ${score}`;
                placeFood();
            } else {
                snake.pop();
            }

            if (checkCollision(head)) {
                endGame();
            }
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
            finalScoreEl.textContent = score;
            gameOverOverlay.style.display = 'flex';
        }

        function draw() {
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, canvas.width, canvas.height);

            ctx.fillStyle = '#0f0';
            ctx.shadowBlur = 10;
            ctx.shadowColor = '#0f0';

            for (let part of snake) {
                ctx.fillRect(part.x * gridSize, part.y * gridSize, gridSize - 2, gridSize - 2);
            }

            ctx.fillStyle = '#f0f';
            ctx.shadowColor = '#f0f';

            ctx.fillRect(food.x * gridSize, food.y * gridSize, gridSize - 2, gridSize - 2);

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
            resetGame();
        });

        initGame();
        draw();
    </script>
</body>
</html>
```