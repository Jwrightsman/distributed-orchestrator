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
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            font-family: 'Courier New', Courier, monospace;
        }

        canvas {
            border: 2px solid #333;
            background-color: #111;
        }

        #score {
            margin-bottom: 10px;
            font-size: 24px;
            color: #fff;
        }

        #gameOverOverlay {
            display: none;
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background-color: rgba(0, 0, 0, 0.9);
            border: 2px solid #f00;
            padding: 40px;
            text-align: center;
            box-shadow: 0 0 20px #f00;
        }

        #gameOverOverlay h2 {
            color: #f00;
            font-size: 48px;
            margin: 0 0 20px 0;
            text-shadow: 0 0 10px #f00;
        }

        #gameOverOverlay p {
            color: #fff;
            font-size: 24px;
            margin: 0 0 20px 0;
        }

        button {
            background-color: #333;
            color: #fff;
            border: 1px solid #fff;
            padding: 10px 20px;
            font-size: 18px;
            cursor: pointer;
            transition: background-color 0.3s;
        }

        button:hover {
            background-color: #555;
        }
    </style>
</head>
<body>
    <div id="score">Score: 0</div>
    <canvas id="gameCanvas" width="400" height="400"></canvas>
    
    <div id="gameOverOverlay">
        <h2>GAME OVER</h2>
        <p id="finalScore">Score: 0</p>
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

        let snake = [];
        let food = {};
        let velocity = { x: 1, y: 0 }; // Moving right on start
        let score = 0;
        let gameRunning = true;
        let gameInterval;

        const foodColors = ['#ff0', '#f0f', '#0ff'];

        function resetGame() {
            snake = [
                { x: 10, y: 10 },
                { x: 9, y: 10 },
                { x: 8, y: 10 }
            ];
            velocity = { x: 1, y: 0 };
            score = 0;
            gameRunning = true;
            scoreEl.textContent = `Score: ${score}`;
            finalScoreEl.textContent = `Score: ${score}`;
            gameOverOverlay.style.display = 'none';
            
            clearInterval(gameInterval);
            gameInterval = setInterval(gameLoop, 100);
            
            placeFood();
        }

        function placeFood() {
            let validPosition = false;
            while (!validPosition) {
                food = {
                    x: Math.floor(Math.random() * tileCountX),
                    y: Math.floor(Math.random() * tileCountY)
                };

                validPosition = true;
                for (let part of snake) {
                    if (part.x === food.x && part.y === food.y) {
                        validPosition = false;
                        break;
                    }
                }
            }
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

        function update() {
            const head = { 
                x: snake[0].x + velocity.x, 
                y: snake[0].y + velocity.y 
            };

            if (checkCollision(head)) {
                endGame();
                return;
            }

            snake.unshift(head);

            if (head.x === food.x && head.y === food.y) {
                score++;
                scoreEl.textContent = `Score: ${score}`;
                placeFood();
            } else {
                snake.pop();
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
            finalScoreEl.textContent = `Score: ${score}`;
            gameOverOverlay.style.display = 'flex';
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

        resetGame();
        draw();
    </script>
</body>
</html>