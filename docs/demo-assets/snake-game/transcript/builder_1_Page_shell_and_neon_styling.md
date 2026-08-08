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
</script>
</body>
</html>